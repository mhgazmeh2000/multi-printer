#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/build_oid_matrix.py — تولید `docs/oid-matrix.md` **از کد** (پیش‌نیاز G.1).

چرا این ابزار هست؟
------------------
پلان فاز G به «ماتریس ۱۵۸ OID در `docs/oid-matrix.md`» ارجاع می‌داد، ولی آن فایل در مخزن
وجود ندارد و عدد ۱۵۸ هیچ‌جای مخزن پیدا نمی‌شود. تصمیم مالک: **مرجع، خودِ کد است**، پس
ماتریس نباید دستی نوشته شود — باید *تولید* شود تا (الف) با واقعیت بخواند، (ب) با هر تغییر
در OIDها بازتولید شود، و (ج) `monitoring/generator.yml` بعداً بتواند با یک اسکریپت
«پوشش کامل» را ثابت کند. هیچ OIDی از خود افزوده نمی‌شود.

منابع (به همین ترتیب):
    • `core/oid/catalog.py::OID_CATALOG` — (oid, type, category, description)
    • `core/snmp/oid_map.py::OIDS`       — نقشهٔ کامل Toshiba (f-string با `OID_BASE`)
    • `core/collectors/*.py` + `core/enhanced_collector.py`

قاعدهٔ شمارش (همه‌اش همین‌جا مستند است، نه در ذهن نویسنده):
    ۱. رشته‌ها با AST خوانده می‌شوند (نه grep) تا f-stringها و ثابت‌های ماژول حل شوند؛
    ۲. helperهای چسباننده (`def gc(s): return g(f"{BASE}.{s}")`) نیز حل می‌شوند — وگرنه
       شمارنده‌های Canon (`1.11.2.1.1.3.1` …) نیمه‌کاره می‌مانند؛
    ۳. فقط رشته‌های **۷ جزء یا بیشتر** OID شمرده می‌شوند؛ پیشوندهای برندیابی
       (`1.3.6.1.4.1.2435`، `1.3.6.1.2.1`، …) در `stats["prefixes"]` گزارش می‌شوند؛
    ۴. ماژول = collector مبدأ (اگر برند-محور باشد)، وگرنه پیشوند enterprise. مثال واقعی:
       شمارنده‌های LBP/MF در `core/collectors/canon.py` زیر شاخهٔ `1.11.…` (درخت HP)
       نشسته‌اند ولی به `printer_canon` تعلق دارند؛
    ۵. dedup روی خودِ OID است؛ تکرارها در §۴ سند فهرست می‌شوند.

خروجی کد: 0 = موفق · 1 = `--check` و سند با کد نمی‌خواند · 2 = منبعی پیدا نشد.

    python tools\\build_oid_matrix.py --report     # خلاصه (توزیع، یتیم‌ها، پیشوندها)
    python tools\\build_oid_matrix.py --write      # تولید docs/oid-matrix.md
    python tools\\build_oid_matrix.py --check       # گیت تازگی (CI/تست)
    python tools\\build_oid_matrix.py --json F      # خروجی ماشینی برای generatorها
"""

from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MATRIX_DOC = PROJECT_ROOT / "docs" / "oid-matrix.md"
SELF = Path(__file__).resolve()

EXIT_OK, EXIT_STALE, EXIT_NO_SOURCE = 0, 1, 2

#: پیشوند enterprise ⇒ ماژول. ترتیب = ترتیب نمایش در سند.
ENTERPRISE_MAP: Sequence[Tuple[str, str]] = (
    ("1.3.6.1.4.1.1129", "printer_toshiba"),
    ("1.3.6.1.4.1.2435", "printer_brother"),
    ("1.3.6.1.4.1.11", "printer_hp"),
    ("1.3.6.1.4.1.1602", "printer_canon"),
    ("1.3.6.1.4.1.47206", "sensor_ecs100g"),
    ("1.3.6.1.2.1", "printer_std"),
)
OTHER_MODULE = "other_unmapped"
MODULE_ORDER: Sequence[str] = [m for _p, m in ENTERPRISE_MAP] + [OTHER_MODULE]

#: پشتیبانِ OIDهایی که هیچ enterprise شناخته‌شده‌ای ندارند: مبدأشان یک collector برند است.
#: (پیشوند enterprise برنده است؛ این جدول فقط «بی‌نام»ها را نجات می‌دهد — وگرنه
#:  OIDهای استانداردِ داخل collectorها مثل `1.3.6.1.2.1.43.11.1.1.6.1` به‌اشتباه
#:  به‌جای `printer_std` در `printer_canon`/`printer_hp` می‌نشستند.)
MODULE_BY_FILE: Dict[str, str] = {
    "core/collectors/hp.py": "printer_hp",
    "core/collectors/canon.py": "printer_canon",
    "core/collectors/brother.py": "printer_brother",
    "core/collectors/toshiba.py": "printer_toshiba",
    "core/collectors/sensor.py": "sensor_ecs100g",
    "core/snmp/oid_map.py": "printer_toshiba",
}

#: این‌ها OID نیستند؛ پیشوند برندیابی‌اند.
NOT_AN_OID = {prefix for prefix, _mod in ENTERPRISE_MAP} | {"1.3.6.1.2.1", "1.3.6.1.4.1"}

#: فایل‌های کمکی — فقط برای سنجهٔ orphan خوانده می‌شوند، نه برای ماتریس.
AUX_FILES: Sequence[str] = (
    "core/poller.py",
    "core/device_classifier.py",
    "core/oid/scanner.py",
    "core/oid/validator.py",
    "core/snmp/protocol.py",
    "core/store.py",
    "create_admin.py",
)
AUX_DIRS: Sequence[str] = ("web", "tools")

OID_RE = re.compile(r"^\d+(?:\.\d+){6,}\.?$")        # ≥۷ جزء ⇒ OID واقعی
ANY_OID_RE = re.compile(r"^\d+(?:\.\d+){2,}\.?$")    # کاندید، شامل پیشوندها
WALK_HINT_RE = re.compile(
    r"(table|_tbl$|^tbl|indexes|tray|supplies|media|input|output|localization|"
    r"seq|list|walk|subtree|candidate|discovery)", re.I)
WALK_CALL_RE = re.compile(r"\b(snmp_walk|snmp_get_bulk|snmp_get_first|snmp_getnext|"
                          r"walk_oids|bulk_walk)\b")

#: پیشوندهای دیده‌شده در اجرا (از شمارش بیرون، در سند گزارش‌شده)
_PREFIXES: Dict[str, set] = {}
#: هر رشتهٔ OID-شکل، حتی «دُم» یا پیشوند — پیمانهٔ خام برای توضیح اعدادِ پلان
_SHAPED: Dict[str, set] = {}
#: مقدار ثابت‌هایی که فقط «پایهٔ ساخت» OIDاند (`OID_BASE`، `BASE`) ⇒ خودشان OID نیستند
_HEADS: set = set()
#: تعداد raw ورودی‌های `OID_CATALOG` (پیش از dedupِ کلیدها) — برای §۱ سند
CATALOG_ROWS = 0


def classify_brand(oid: str) -> str:
    for prefix, name in ENTERPRISE_MAP:
        if oid == prefix or oid.startswith(prefix + "."):
            return name
    return OTHER_MODULE


def module_for(oid: str, src_file: str) -> str:
    """پیشوند enterprise برنده است؛ جدول فایل فقط بی‌نام‌ها را نجات می‌دهد."""
    mod = classify_brand(oid)
    return mod if mod != OTHER_MODULE else MODULE_BY_FILE.get(src_file, OTHER_MODULE)


def _module_constants(tree: ast.Module) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        out[tgt.id] = node.value.value
    return out


def _fstring(value: ast.JoinedStr, consts: Dict[str, str]) -> Optional[str]:
    """f-string تنها وقتی حل می‌شود که همهٔ اجزا ثابت یا Nameِ ماژول‌شناخته باشند."""
    parts: List[str] = []
    for part in value.values:
        if isinstance(part, ast.Constant):
            if not isinstance(part.value, str):
                return None
            parts.append(part.value)
        elif isinstance(part, ast.FormattedValue):
            inner = part.value
            if isinstance(inner, ast.Name) and inner.id in consts:
                parts.append(consts[inner.id])
            elif isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                parts.append(inner.value)
            else:
                return None
        else:
            return None
    return "".join(parts)


def _scope_consts(node: ast.AST) -> Dict[str, str]:
    """ثابت‌های رشته‌ایِ در دسترسِ یک گره: scope خودش، بعد والدین، بعد سطح ماژول."""
    out: Dict[str, str] = {}
    scopes: List[ast.AST] = []
    cur: Optional[ast.AST] = node
    while cur is not None:
        scopes.append(cur)
        cur = getattr(cur, "_parent", None)
    for scope in scopes:
        if not isinstance(scope, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for assign in [n for n in ast.walk(scope) if isinstance(n, ast.Assign)]:
            if not isinstance(assign.value, ast.Constant) or not isinstance(assign.value.value, str):
                continue
            for tgt in assign.targets:
                if isinstance(tgt, ast.Name) and tgt.id not in out:
                    out[tgt.id] = assign.value.value      # نزدیک‌ترین scope اول می‌خواند
    return out


def _fstring_head(node: ast.JoinedStr, consts: Dict[str, str]) -> str:
    """اگر f-string با یک placeholder شروع شود، مقدارِ آن ثابتِ اول (و ثبت در _HEADS)."""
    if not node.values:
        return ""
    first = node.values[0]
    if isinstance(first, ast.FormattedValue) and isinstance(first.value, ast.Name):
        head = consts.get(first.value.id, "")
        if head:
            _HEADS.add(head)
        return head
    return ""


def _composers(tree: ast.Module, consts: Dict[str, str]) -> Dict[str, str]:
    """`def gc(s, ...): return g(f"{BASE}.{s}")` ⇒ `gc("x")` == `BASE.x`.

    دو نکته که در این مخزن لازم است: (الف) `gc` **تو-تودر** است (`collect_canon` آن را در
    بدنه‌اش تعریف می‌کند) ⇒ همهٔ FunctionDefها دیده می‌شوند، نه فقط سطح ماژول؛ (ب) `BASE`
    در canon.py:124 داخل تابع والد است ⇒ جست‌وجو از scope خودِ تابع به بالا.
    """
    out: Dict[str, str] = {}
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        # هر تابع جدول ثابت‌های خودش را می‌بیند (نشت BASE بین توابع = OID غلط)
        consts = dict({**consts, **_scope_consts(fn)})
        body = fn.body[0] if len(fn.body) == 1 else None
        if not (isinstance(body, ast.Return) and isinstance(body.value, ast.Call)):
            continue
        inner = body.value
        if not (inner.args and isinstance(inner.args[0], ast.JoinedStr)):
            continue
        parts = list(inner.args[0].values)
        if len(parts) != 3 or not (isinstance(parts[1], ast.Constant) and parts[1].value == "."):
            continue
        head, tail = parts[0], parts[2]
        if not (isinstance(head, ast.FormattedValue) and isinstance(head.value, ast.Name)
                and head.value.id in consts):
            continue
        params = [a.arg for a in fn.args.args]
        if not (isinstance(tail, ast.FormattedValue) and isinstance(tail.value, ast.Name)
                and params and tail.value.id == params[0]):
            continue
        out[fn.name] = consts[head.value.id]
    return out


def _annotate_parents(tree: ast.Module) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # type: ignore[attr-defined]


def _dict_key_label(child: ast.AST, dic: ast.Dict) -> str:
    """اگر مقدارِ یک کلیدِ dict باشد، همان کلید بهترین برچسب است (core/snmp/oid_map.py)."""
    for k, v in zip(dic.keys, dic.values):
        if v is child and isinstance(k, ast.Constant) and isinstance(k.value, str):
            return k.value
    return ""


def _label_from_context(node: ast.AST) -> str:
    """برچسب خوانا: کلیدِ dict، نام متغیرِ assign، کلید keyword، یا تابع میزبان."""
    parent = getattr(node, "_parent", None)
    # کلیدِ dict نزدیک‌تر و دقیق‌تر از نامِ متغیرِ دیکشنری است (`OIDS` → `toner_black_status`)
    cur, up = node, parent
    while up is not None and not isinstance(up, (ast.Assign, ast.AnnAssign)):
        if isinstance(up, ast.Dict):
            key = _dict_key_label(cur, up)
            if key:
                return key
            break
        cur, up = up, getattr(up, "_parent", None)
    best = ""
    while parent is not None:
        if isinstance(parent, ast.Assign):
            for tgt in parent.targets:
                if isinstance(tgt, ast.Name):
                    return tgt.id
            break
        if isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
            return parent.target.id
        if isinstance(parent, ast.keyword):
            return parent.arg or best
        if isinstance(parent, ast.FunctionDef):
            best = best or f"call:{parent.name}"
        parent = getattr(parent, "_parent", None)
    return best or "(inline)"


def _container_name(node: ast.AST) -> str:
    """نام متغیرِ assignی که این ردیف داخلش نوشته شده (`CANDIDATE_SUBTREES = {...}`).

    برچسبِ ردیف‌های dict از کلیدشان می‌آید (`hp`)؛ برای حدسِ `walk` از کجا خوانده شده
    نامِ ظرف هم لازم است، وگرنه `CANDIDATE_SUBTREES` (که نشانهٔ پویش است) گم می‌شود.
    """
    cur = getattr(node, "_parent", None)
    while cur is not None:
        if isinstance(cur, ast.Assign):
            for tgt in cur.targets:
                if isinstance(tgt, ast.Name):
                    return tgt.id
            return ""
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ""
        cur = getattr(cur, "_parent", None)
    return ""


def _nearest_call(node: ast.AST) -> str:
    cur = getattr(node, "_parent", None)
    while cur is not None:
        if isinstance(cur, ast.Call):
            fn = cur.func
            return getattr(fn, "attr", None) or getattr(fn, "id", None) or ""
        cur = getattr(cur, "_parent", None)
    return ""


def scan_file(path: Path) -> List[Dict[str, str]]:
    """هر رشتهٔ OID-مانند در فایل: {oid, line, label, kind, file} — dedup درون‌فایلی."""
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    tree = ast.parse(src)
    _annotate_parents(tree)
    consts = _module_constants(tree)
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    walk_lines = {n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and WALK_CALL_RE.search(_call_name(n))}
    out: List[Dict[str, str]] = []
    seen = set()
    #: خط ⇒ «دُم‌هایی» که آن خط با helper کامل شده‌اند؛ نسخهٔ کامل شمرده می‌شود، دُم نه
    suppressed: Dict[int, set] = {}

    def emit(text: str, lineno: int, label: str, node: ast.AST, head: str = "") -> None:
        oid = text.strip().rstrip(".")
        if ANY_OID_RE.match(oid) and len(oid.split(".")) >= 7:
            _SHAPED.setdefault(oid, set()).add(f"{rel}:{lineno}")
        if oid in suppressed.get(lineno, ()):
            return          # این رشته «دُم» است؛ نسخهٔ کاملش از gc() ثبت شده
        if oid in NOT_AN_OID or not ANY_OID_RE.match(oid):
            return
        if not OID_RE.match(oid):
            _PREFIXES.setdefault(oid, set()).add(f"{rel}:{lineno}")
            return
        key = (oid, lineno, label)
        if key in seen:
            return
        seen.add(key)
        container = _container_name(node)
        near_walk = any(abs(wl - lineno) <= 6 for wl in walk_lines)
        is_walk = (near_walk or bool(WALK_HINT_RE.search(f"{label} {container}"))
                   or bool(WALK_CALL_RE.search(_nearest_call(node))))
        # head: اگر OID از چسبیدن یک ثابت (مثل OID_BASE یا BASE) به دُم ساخته شده،
        # مقدار همان ثابت اینجا می‌آید؛ تست §۳ مخزن می‌سنجد که «سرتاسرِ OID اختراع نشده»
        if head and not oid.startswith(head):
            head = ""
        row = {"oid": oid, "line": str(lineno), "label": label,
               "kind": "walk" if is_walk else "get", "file": rel, "head": head}
        if container:
            row["container"] = container   # نام ظرف (برای نام‌گذاری متریک در generator.yml)
        out.append(row)

    # تکه‌های یک f-stringِ حل‌نشده (مثل `f"…110.{port}.2.0"`) OID کامل نیستند
    inside_fstring = {id(c) for j in ast.walk(tree) if isinstance(j, ast.JoinedStr)
                      for c in ast.walk(j) if isinstance(c, ast.Constant)}
    for oid_c, call_node, label_c, base_c in _collect_composed(tree, _composers(tree, consts)):
        suppressed.setdefault(call_node.lineno, set()).add(oid_c.split(".", 7)[-1])
        emit(oid_c, call_node.lineno, _label_from_context(call_node) or label_c,
             call_node, head=base_c)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in inside_fstring:
                continue
            emit(node.value, node.lineno, _label_from_context(node), node)
        elif isinstance(node, ast.JoinedStr):
            resolved = _fstring(node, consts)
            if resolved:
                emit(resolved, node.lineno, _label_from_context(node), node,
                     head=_fstring_head(node, consts))
    return out


def _call_name(call: ast.Call) -> str:
    fn = call.func
    return str(getattr(fn, "attr", None) or getattr(fn, "id", None) or "")


def _collect_composed(tree: ast.Module,
                      composers: Dict[str, str]) -> List[Tuple[str, int, str, str]]:
    """`(OID کامل, گره فراخوان, برچسبِ پشتیبان, مقدار BASE)`.

    برچسب از نام متغیرِ assign ساخته می‌شود (`alt_total = si(gc("…"), -1)` ⇒ `alt_total`)؛
    اگر قابل تشخیص نبود برچسب پشتیبان `composed:<helper>` می‌ماند. `BASE` هم می‌آید تا
    تستِ مخزن بتواند بسنجد OID اختراع نشده است.
    """
    out: List[Tuple[str, ast.Call, str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in composers and node.args):
            continue
        arg0 = node.args[0]
        if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
            base = composers[node.func.id]
            _HEADS.add(base)
            out.append((f"{base}.{arg0.value.strip().rstrip('.')}", node,
                        f"composed:{node.func.id}", base))
    return out


def catalog_entries(path: Path) -> Dict[str, Tuple[str, str, str, int]]:
    """`OID_CATALOG` ساختاری: oid ⇒ (key, type, category, line)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    _annotate_parents(tree)
    consts = _module_constants(tree)
    out: Dict[str, Tuple[str, str, str, int]] = {}
    rows_seen = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.endswith("CATALOG") for t in node.targets)):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for k, v in zip(node.value.keys, node.value.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            if not (isinstance(v, ast.Tuple) and v.elts and isinstance(v.elts[0], ast.Constant)):
                continue
            oid = v.elts[0].value
            if isinstance(v.elts[0], ast.JoinedStr):
                oid = _fstring(v.elts[0], consts)
            if not isinstance(oid, str):
                continue
            oid = oid.strip().rstrip(".")
            if not OID_RE.match(oid):
                continue
            typ = v.elts[1].value if len(v.elts) > 1 and isinstance(v.elts[1], ast.Constant) else ""
            cat = v.elts[2].value if len(v.elts) > 2 and isinstance(v.elts[2], ast.Constant) else ""
            out[oid] = (k.value, str(typ), str(cat), v.lineno)
            rows_seen += 1
    global CATALOG_ROWS
    CATALOG_ROWS = rows_seen
    return out


def primary_sources() -> Tuple[List[Path], Path, Path]:
    collectors = sorted((PROJECT_ROOT / "core" / "collectors").glob("*.py"))
    files = [p for p in collectors + [PROJECT_ROOT / "core" / "enhanced_collector.py"]
             if p.is_file()]
    return files, PROJECT_ROOT / "core" / "oid" / "catalog.py", PROJECT_ROOT / "core" / "snmp" / "oid_map.py"


def build() -> Tuple[Dict[str, List[Dict[str, str]]], Dict[str, object]]:
    files, cat_path, omap_path = primary_sources()
    if not files or not cat_path.is_file():
        raise FileNotFoundError("منابع پیدا نشد؛ ابزار را از ریشهٔ مخزن اجرا کنید")

    cat = catalog_entries(cat_path)
    rows: Dict[str, Dict[str, Dict[str, str]]] = collections.defaultdict(dict)

    # ۱) کاتالوگ — غنی‌ترین اطلاعات، پس اولویت اول
    for oid, (key, typ, category, line) in cat.items():
        mod = classify_brand(oid)
        kind = "walk" if category in ("supply", "tray") or WALK_HINT_RE.search(key) else "get"
        rows[mod][oid] = {"oid": oid, "label": key, "kind": kind, "type": typ, "category": category,
                          "file": cat_path.relative_to(PROJECT_ROOT).as_posix(), "line": str(line),
                          "origin": "catalog"}

    # ۲) oid_map (Toshiba) و collectorها
    for path in [omap_path] + files:
        if not path.is_file():
            continue
        for r in scan_file(path):
            oid, mod = r["oid"], module_for(r["oid"], r["file"])
            if oid in rows[mod]:
                rows[mod][oid].setdefault("also", [])
                loc = f"{r['file']}:{r['line']}"
                if loc not in rows[mod][oid]["also"]:
                    rows[mod][oid]["also"].append(loc)
                continue
            r["type"] = ""
            r["category"] = ""
            r["origin"] = "code"
            rows[mod][oid] = r

    def _num(oid: str) -> List[int]:
        return [int(x) for x in oid.split(".")]

    # «پایهٔ ساخت» از ماتریس بیرون می‌رود: `OID_BASE`/`BASE` هرگز به‌تنهایی خوانده
    # نمی‌شوند، فقط پیشوندِ f-stringهایند. (سنجهٔ «پدرِ ≥۳ بچه» عمداً استفاده نشد:
    # subtreeهای کاوش مثل `…2435.2.3.9.4.2.1.5` واقعی‌اند و باید در walk بمانند.)
    dropped: Dict[str, str] = {}
    for mod, table in rows.items():
        for oid in list(table):
            is_base = oid in _HEADS
            if is_base:
                dropped[oid] = table[oid]["label"]
                del table[oid]

    modules = {m: sorted(v.values(), key=lambda x: _num(x["oid"])) for m, v in rows.items()}
    unique = {o for v in rows.values() for o in v}
    occurrences = 0
    for path in [omap_path, cat_path] + files:
        if path.is_file():
            occurrences += len(scan_file(path))

    # ۳) یتیم‌ها: OIDهایی که فقط در کدِ غیرمرجع‌اند
    aux: Dict[str, List[str]] = collections.defaultdict(list)
    for rel in AUX_FILES:
        p = PROJECT_ROOT / rel
        if p.is_file():
            for r in scan_file(p):
                if r["oid"] not in unique:
                    aux[r["oid"]].append(f"{r['file']}:{r['line']}")
    for d in AUX_DIRS:
        for p in sorted(PROJECT_ROOT.joinpath(d).glob("**/*.py")):
            if p.resolve() == SELF:
                continue                      # خودِ این ابزار (regexهایش OID نیستند)
            for r in scan_file(p):
                if r["oid"] not in unique:
                    aux[r["oid"]].append(f"{p.relative_to(PROJECT_ROOT).as_posix()}:{r['line']}")
    orphans = {o: sorted(set(v)) for o, v in sorted(aux.items(), key=lambda kv: _num(kv[0]))}

    # ۴) شمارش دوم: ماتریس + probeها — برای سنجش «۱۵۸» پلان
    all_codes: Dict[str, set] = collections.defaultdict(set)
    for mod_name, oid_rows in rows.items():
        all_codes[mod_name] |= set(oid_rows)
    for oid in orphans:
        all_codes[classify_brand(oid)].add(oid)
    with_probes = {m: len(v) for m, v in sorted(all_codes.items())}
    catalog_only = sorted(
        {o for v in rows.values() for o, r in v.items() if r.get("origin") == "catalog"}
        - {r["oid"] for path in [omap_path] + files if path.is_file() for r in scan_file(path)})

    stats = {
        "unique_oids": len(unique),
        "catalog_entries": len(cat),
        "catalog_rows": CATALOG_ROWS,
        "occurrences": occurrences,
        "per_module": {m: len(v) for m, v in sorted(modules.items())},
        "walks": sum(1 for m in modules.values() for r in m if r["kind"] == "walk"),
        "gets": sum(1 for m in modules.values() for r in m if r["kind"] == "get"),
        "orphans": orphans,
        "per_module_with_probes": with_probes,
        "with_probes_total": len({o for v in all_codes.values() for o in v}),
        "prefixes": {k: sorted(v)[:3] for k, v in sorted(_PREFIXES.items())},
        "bases_dropped": dropped,
        "shaped_total": len(_SHAPED),
        "shaped_not_in_matrix": len({o for o in _SHAPED} - unique),
        "from_catalog_only": catalog_only,
    }
    return modules, stats


def render_doc(modules: Dict[str, List[Dict[str, str]]], stats: Dict[str, object]) -> str:
    """Markdown قطعی (بدون timestamp) تا `--check` معنا داشته باشد."""
    lines: List[str] = []
    a = lines.append
    a("# ماتریس OID — تولیدشده از کد")
    a("")
    a("> ⚙️ خروجی `tools/build_oid_matrix.py` است؛ **دستی ویرایش نکنید**.  ")
    a("> بازتولید: `python tools\\build_oid_matrix.py --write` · گیت تازگی: "
      "`python tools\\build_oid_matrix.py --check` (کد خروج ۱ ⇒ سند کهنه است).")
    a("")
    a("## ۱. سرجمع")
    a("")
    a(f"- **{stats['unique_oids']}** OID یکتا در ماتریس — مرجع `monitoring/generator.yml`.")
    a(f"- `core/oid/catalog.py::OID_CATALOG`: **{stats['catalog_rows']}** ورودی، که "
      f"{stats['catalog_entries']} تای آن‌ها OID یکتا می‌دهد "
      f"({stats['catalog_rows'] - stats['catalog_entries']} OID تکراری برای fallback)")
    a(f"- نوشتارِ متن OID در فایل‌های مرجع (occurrence): {stats['occurrences']} — یک OID "
      f"می‌تواند در چند collector/فهرست آمده باشد")
    a(f"- نوع خواندن: `get` = {stats['gets']} · `walk` = {stats['walks']}")
    a(f"- OID یتیم (فقط در کدِ کمکی/وب/ابزار): {len(stats['orphans'])}")
    a(f"- با احتساب probeها: {stats['with_probes_total']}")
    a(f"- پیمانهٔ خام (هر رشتهٔ OID-شکل، حتی دُم/پیشوند/probe): {stats['shaped_total']} — "
      f"{stats['shaped_not_in_matrix']} تای آن در ماتریس **نیست** و مبنای `generator.yml` "
      f"نمی‌شود")
    a(f"- پایهٔ ساخت که از ماتریس بیرون رفت (`OID_BASE`/`BASE`): {len(stats['bases_dropped'])} — "
      + (", ".join(f"`{o}`" for o in list(stats['bases_dropped'])[:6]) or "—"))
    a(f"- رشته‌هایی که OID شمرده **نشده‌اند**: {len(stats['prefixes'])} جدول در §۶ "
      "(کوتاه‌تر از ۷ جزء، یا پیشوند برند‌یابی/regex)")
    a("")
    a("### چرا «یک عدد» نیست")
    a("")
    a("| پیمانه | عدد | چیست |")
    a("|---|---|---|")
    a(f"| OID یکتا | {stats['unique_oids']} | مجموعهٔ ریاضی OIDهایی که کد واقعاً می‌خواند |")
    a(f"| ردیف کاتالوگ | {stats['catalog_entries']} | هر ورودیِ `OID_CATALOG` یک سطر (OID تکراری هم دارد) |")
    a(f"| occurrence | {stats['occurrences']} | هر بار که رشتهٔ OID در کدِ مرجع نوشته شده |")
    a("")
    a("> هر عدد دیگری (مثل **۱۵۸** در پلان فاز G) باید به یکی از این سه پیمانه برگردد، وگرنه "
      "یا ردیف‌های گستردهٔ `walk × index` شمرده شده یا تکرارها. `generator.yml` با پیمانهٔ "
      "**اول** ساخته و سنجیده می‌شود.")
    a("")
    a("## ۲. توزیع به تفکیک ماژول")
    a("")
    a("| ماژول | OID یکتا | `get` | `walk` | + probe |")
    a("|---|---|---|---|---|")
    for mod in MODULE_ORDER:
        rows_m = modules.get(mod) or []
        if not rows_m:
            continue
        w = sum(1 for r in rows_m if r["kind"] == "walk")
        wp = stats["per_module_with_probes"].get(mod, 0)
        a(f"| `{mod}` | {len(rows_m)} | {len(rows_m) - w} | {w} | {wp} |")
    a(f"| **جمع** | **{stats['unique_oids']}** | **{stats['gets']}** | **{stats['walks']}** | "
      f"**{stats['with_probes_total']}** |")
    a("")
    a("ستون «+ probe» شامل `web/routes/*` و `tools/*` (کاوش‌گرها و تشخیص‌دهنده‌ها) هم هست؛ "
      "مبنای `generator.yml` ستون دوم است، چون مسیر polling همین کد است.")
    a("")
    a("## ۳. ردیف‌ها")
    a("")
    prefixes = dict(ENTERPRISE_MAP)
    for mod in MODULE_ORDER:
        rows_m = modules.get(mod) or []
        if not rows_m:
            continue
        prefix = prefixes.get(mod, "—")
        a(f"### `{mod}`" + (f" — پیشوند `{prefix}`" if prefix != "—" else "") +
          f" · {len(rows_m)} OID")
        a("")
        a("| OID | خواندن | برچسب/کلید | دسته | نوع | منبع |")
        a("|---|---|---|---|---|---|")
        for r in rows_m:
            cat = r.get("category") or ("toshiba_map" if r["file"].endswith("oid_map.py") else "code")
            src = f"`{r['file']}:{r['line']}`"
            also = r.get("also") or []
            if also:
                src += f" (+{len(also)})"
            a(f"| `{r['oid']}` | {r['kind']} | {r['label'] or '—'} | {cat} | {r.get('type') or '—'} | {src} |")
        a("")
    a("## ۴. تکرارها (یک OID، چند جا)")
    a("")
    dupes = [(mod, r) for mod in MODULE_ORDER for r in (modules.get(mod) or []) if r.get("also")]
    if not dupes:
        a("هیچ OIDی در فایل‌های مرجع تکرار نشده است.")
    else:
        a("| OID | ماژول | تکرارها |")
        a("|---|---|---|")
        for mod, r in dupes:
            shown = " · ".join(f"`{x}`" for x in r["also"][:4])
            a(f"| `{r['oid']}` | `{mod}` | {shown}{' …' if len(r['also']) > 4 else ''} |")
    a("")
    a("## ۵. یتیم‌ها")
    a("")
    a("این OIDها در **کد غیرمرجع** (routeها، ابزارها، کاوشگرها) خوانده می‌شوند نه در "
      "کاتالوگ/collector — فهرست برای این است که معلوم شود کدام‌ها باید به کاتالوگ اضافه شوند:")
    a("")
    if not stats["orphans"]:
        a("— هیچ؛ هر OIDی که کد مرجع می‌خواند در کاتالوگ یا collector تعریف شده است.")
    else:
        a("| OID | ماژول | محل |")
        a("|---|---|---|")
        for oid, where in stats["orphans"].items():
            a(f"| `{oid}` | `{classify_brand(oid)}` | {' · '.join(f'`{w}`' for w in where[:3])} |")
    a("")
    a("## ۶. پیشوندهایی که OID شمرده نشد")
    a("")
    a("| پیشوند | نمونهٔ محل | چرا شمرده نشد |")
    a("|---|---|---|")
    for pre, where in stats["prefixes"].items():
        a(f"| `{pre}` | {' · '.join(f'`{w}`' for w in where[:2])} | "
          f"یا پیشوند برندیابی است یا «دُم» یک f-string/تابع ⇒ OID کاملِ یک برگ نیست |")
    a("")
    a(f"از کاتالوگ، **{len(stats['from_catalog_only'])}** OID فقط در `OID_CATALOG` آمده و در "
      f"collectorها مستقیم نوشته نشده — اینها هم در ماتریس‌اند (کاتالوگ بخشی از قرارداد است).")
    a("")
    a("## ۷. چطور مصرف شود")
    a("")
    a("- `monitoring/generator.yml`: ماژول‌ها = §۲؛ هر ردیف §۳ باید در آن باشد. سنجهٔ پوشش: "
      "`tools/check_oid_coverage.py` (G.2) — اگر OIDی در ماتریس بود و در generator نبود، "
      "یا برعکس، CI قرمز است.")
    a("- نام متریک در generator از ستون «برچسب/کلید» می‌آید؛ اگر برچسب یک کلمهٔ مبهم باشد "
      "(نام برند یا کوتاه‌تر از ۶ حرف)، نام ظرف پیشوند می‌شود "
      "(`brother` داخل `CANDIDATE_SUBTREES` ⇒ `candidate_subtrees_brother`).")
    a("- یتیم‌های §۵ در generator فقط **کامنت**‌اند (probe-only) و گیت مراقب است که متریک "
      "زنده نشوند؛ پوششِ یک یتیم با walk زیر درختش مجاز است (std: `43.11.1.1`).")
    a("- OID تازه: اول کد (کاتالوگ/collector)، بعد `--write`. سند هیچ‌وقت جلوتر از کد نمی‌رود.")
    a("")
    body = "\n".join(lines).rstrip() + "\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return body + f"<!-- sha256:{digest} · tools/build_oid_matrix.py -->\n"


def _report(modules, stats) -> str:
    out = ["══ توزیع per brand (OID یکتا) ══"]
    for mod in MODULE_ORDER:
        rows_m = modules.get(mod) or []
        if rows_m:
            w = sum(1 for r in rows_m if r["kind"] == "walk")
            out.append(f"  {mod:22s} {len(rows_m):4d}  (get {len(rows_m) - w:3d} · walk {w:2d} · "
                       f"+probe {stats['per_module_with_probes'].get(mod, 0)})")
    out.append(f"  {'جمع':22s} {stats['unique_oids']:4d}  "
               f"[catalog {stats['catalog_entries']} · occurrence {stats['occurrences']} · "
               f"با probe {stats['with_probes_total']}]")
    out.append("")
    out.append(f"══ یتیم‌ها ({len(stats['orphans'])}) ══")
    for oid, where in stats["orphans"].items():
        out.append(f"  {oid:46s} ← {' · '.join(where[:3])}")
    out.append("")
    out.append(f"══ پیشوندهای شمرده‌نشده ({len(stats['prefixes'])}) ══")
    for pre, where in stats["prefixes"].items():
        out.append(f"  {pre:32s} ← {', '.join(where)}")
    out.append("")
    out.append(f"══ فقط-کاتالوگی: {len(stats['from_catalog_only'])} ══")
    out.append("")
    out.append(f"══ پایه‌هایی که از ماتریس بیرون رفتند ({len(stats['bases_dropped'])}) ══")
    for oid, label in stats["bases_dropped"].items():
        out.append(f"  {oid:40s} ← {label}")
    out.append("")
    out.append(f"══ پیمانهٔ خام: هر رشتهٔ OID-شکل در مرجع+وب+ابزارها = {stats['shaped_total']} ══")
    out.append(f"   که {stats['shaped_not_in_matrix']} تا در ماتریس نیستند (دُم/پیشوند/probe)")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="تولید docs/oid-matrix.md از کد (مرجع G.1)")
    ap.add_argument("--write", action="store_true", help="سند را بنویس")
    ap.add_argument("--check", action="store_true", help="بررسی تازگی سند")
    ap.add_argument("--report", action="store_true", help="خلاصه روی stdout")
    ap.add_argument("--json", dest="json_path", default="", help="خروجی ماشینی")
    args = ap.parse_args(argv)

    try:
        modules, stats = build()
    except FileNotFoundError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return EXIT_NO_SOURCE

    doc = render_doc(modules, stats)
    if args.report:
        print(_report(modules, stats))
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(
            {"stats": {k: v for k, v in stats.items()}, "modules": modules},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"🧾 JSON: {args.json_path}")
    if args.check:
        if not MATRIX_DOC.is_file():
            print("✘ docs/oid-matrix.md وجود ندارد (اول --write)", file=sys.stderr)
            return EXIT_STALE
        if MATRIX_DOC.read_text(encoding="utf-8").strip() != doc.strip():
            print("✘ سند کهنه است؛ با --write بازتولید کنید", file=sys.stderr)
            return EXIT_STALE
        print("✔ docs/oid-matrix.md برابرِ خروجی کد است")
        return EXIT_OK
    if args.write:
        MATRIX_DOC.write_text(doc, encoding="utf-8")
        print(f"✔ نوشته شد: {MATRIX_DOC.relative_to(PROJECT_ROOT).as_posix()} — {stats['unique_oids']} OID "
              f"یکتا، {len(doc.splitlines())} خط")
        return EXIT_OK
    if not (args.report or args.json_path):
        print(doc)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
