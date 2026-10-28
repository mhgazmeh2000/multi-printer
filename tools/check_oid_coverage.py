#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/check_oid_coverage.py
# @brief:        Count gate that checks monitoring/generator.yml against docs/oid-matrix.md.
# @details:      Responsibilities: Read the matrix, compare coverage and fail on mismatch.
#                Main API: read_matrix(), lcp(), needs_ignore(), check(), main()
#                Side effects: none at import time
# @dependencies: external: yaml
# @notes:        Can be run directly as a script.
# =============================================================================

"""
tools/check_oid_coverage.py — count gate: `monitoring/generator.yml` ⊡ `docs/oid-matrix.md`.

What does "100% coverage" mean? (defined here, not elsewhere)
    1. The generator modules are exactly the modules of §2 of the matrix;
    2. In each module: {oids `get`} ∪ {oids `walk[].metrics`} = rows of the same matrix
       module — not too few, not too many, and no rows used twice;
    3. Any *live* OID in the file (ie any value inside YAML, not comments) or matrix row
       is or `subtree` the same as the input of walk — and `subtree` must be exactly the longest prefix
       be common to its own rows (so "add OID without consumer" is not possible);
    4. `lookups[].label_oid` are rows of the same module in the matrix (map index→description
       comes from code, not imagination);
    5. `source:' each metric must be exactly the same as the `source' of its own row in the matrix;
    6. `ignore_values: ["-2", "-3"]` on exactly the same metrics as the document rule
       says (function `needs_ignore`; string metrics never get sentinel);
    7. orphaned OIDs (matrix §5) only appear in **comments**, not as live metrics;
    8. `auths` = public_v1 + public_v2 + secure_v3, and any template v3 credentials
       be `${VAR_ENV}` (the password does not sit in git).

Exit code: 0 = all true · 1 = failure (messages on stderr) · 2 = file/document not found.
    python tools\\check_oid_coverage.py # for Adam
    python tools\\check_oid_coverage.py --json # for CI
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MATRIX_DOC = PROJECT_ROOT / "docs" / "oid-matrix.md"
GENERATOR = PROJECT_ROOT / "monitoring" / "generator.yml"

EXIT_OK, EXIT_FAIL, EXIT_MISSING = 0, 1, 2

OID_RE = re.compile(r"^\d+(?:\.\d+){6,}$")
MODULE_RE = re.compile(r"^### `([a-z0-9_]+)`")
ROW_RE = re.compile(r"^\|\s*`(\d+(?:\.\d+){6,})`\s*\|\s*(get|walk)\s*\|\s*([^|]*?)\s*\|"
                    r"\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*`([^`:]+):(\d+)`")
ORPHAN_RE = re.compile(r"^\|\s*`(\d+(?:\.\d+){6,})`\s*\|\s*`([a-z0-9_]+)`\s*\|\s*([^|]+)\|")
PROBE_COMMENT_RE = re.compile(r"^\s*#\s*(\d+(?:\.\d+){6,})\s+←", re.M)
ENV_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Z0-9_]+\}$")

#: Metrics that have "not value" sentinels — explicit rule, so that the gate is debatable:
#: (a) catalog row with category ``supply'', or
#: (b) metric name means percentage/level/capacity, or
#: (c) the source is `core/enhanced_collector.py` — there they are all read from a loop that
#: only accepts `0 <= v <= 100` (enhanced_collector.py:243-249 and 244-250).
#: Evidence in code: hp.py:271,273 (`lvl == -2` / `lvl == -3`), brother.py:196-197,
#: canon.py:216 (fallback to -1), brother.py:25 (explanation of level=-3 in NC family).
IGNORE_NAME_RE = re.compile(r"(level|remain|cap|max|pct|toner|drum|black)", re.I)
SENTINELS = ["-2", "-3"]


def read_matrix(doc_path: Path) -> Tuple[Dict[str, Dict[str, Dict[str, str]]], Set[str]]:
    """§3 reads the rows and §5 orphans from the document."""
    text = doc_path.read_text(encoding="utf-8")
    modules: Dict[str, Dict[str, Dict[str, str]]] = {}
    current: Optional[str] = None
    in_orphans = False
    orphans: Set[str] = set()
    for line in text.splitlines():
        m = MODULE_RE.match(line)
        if m:
            current = m.group(1)
            modules.setdefault(current, {})
            in_orphans = False
            continue
        if line.startswith("## ۵"):
            in_orphans = True
            current = None
            continue
        if line.startswith("## ۶"):
            in_orphans = False
        r = ROW_RE.match(line)
        if r and current:
            oid, kind, label, category, typ, src_file, src_line = r.groups()
            modules[current][oid] = {"kind": kind, "label": label.strip(),
                                     "category": category.strip(), "type": typ.strip(),
                                     "file": src_file.strip(), "line": src_line.strip()}
            continue
        o = ORPHAN_RE.match(line)
        if o and in_orphans:
            orphans.add(o.group(1))
    return modules, orphans


def lcp(oids: Sequence[str]) -> str:
    """Return the longest common prefix of a list of OIDs."""

    parts = [o.split(".") for o in oids]
    out: List[str] = []
    for tup in zip(*parts):
        if len(set(tup)) != 1:
            break
        out.append(tup[0])
    return ".".join(out)


def needs_ignore(row: Dict[str, str], name: str) -> bool:
    """Sentinel Rule: Numeric metrics only. Strings (`type: str` / DisplayString) never
    `-2`/`-3` do not take — eg `toner_name_1` is the name of the cartridge, not the percentage."""
    if row.get("type") == "str":
        return False
    if row.get("category") == "supply":
        return True
    if IGNORE_NAME_RE.search(name or ""):
        return True
    return row.get("file", "").endswith("enhanced_collector.py")


def _walk_strings(node: object, path: str = "") -> Iterable[Tuple[str, object]]:
    """All string values ​​inside the YAML structure (comments do not come here)."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def check(doc_path: Path, gen_path: Path) -> Tuple[List[str], Dict[str, object]]:
    """Compare the OID matrix with generator.yml and return the problems found."""

    problems: List[str] = []
    if not doc_path.is_file():
        return [f"سند ماتریس نیست: {doc_path}"], {}
    if not gen_path.is_file():
        return [f"فایل ژنراتور نیست: {gen_path}"], {}

    try:
        import yaml                                  # noqa: WPS433 (dev dependency)
    except ImportError:
        return ["PyYAML نصب نیست: pip install -r requirements-dev.txt"], {}

    matrix, orphans = read_matrix(doc_path)
    gen = yaml.safe_load(gen_path.read_text(encoding="utf-8"))
    if not isinstance(gen, dict):
        return ["generator.yml یک mapping نیست"], {}

    modules = gen.get("modules")
    if not isinstance(modules, dict):
        return ["`modules:` وجود ندارد یا mapping نیست"], {}

    # 1) Modules
    want = {m for m, rows in matrix.items() if rows}
    got = set(modules)
    if want - got:
        problems.append(f"ماژول‌های جاافتاده: {sorted(want - got)}")
    if got - want:
        problems.append(f"ماژولِ بی‌منبع در generator: {sorted(got - want)}")

    all_matrix_oids = {o for rows in matrix.values() for o in rows}
    used: Set[str] = set()
    metric_oids: Set[str] = set()      # metric oids only; `subtree' does not come here
    per_module: Dict[str, Dict[str, int]] = {}
    ignore_actual: Set[str] = set()

    for mod, rows in matrix.items():
        cfg = modules.get(mod)
        if not isinstance(cfg, dict):
            continue
        got_oids: Dict[str, str] = {}
        for entry in cfg.get("get") or []:
            oid = str(entry.get("oid", ""))
            got_oids[oid] = "get"
            if entry.get("ignore_values"):
                if list(entry["ignore_values"]) != SENTINELS:
                    problems.append(f"{mod}/{entry.get('name')}: ignore_values باید {SENTINELS} "
                                    f"باشد، نه {entry['ignore_values']}")
                ignore_actual.add(f"{mod}/{entry.get('name', '')}")
        for wentry in cfg.get("walk") or []:
            metrics = wentry.get("metrics") or []
            if not metrics:
                problems.append(f"{mod}: walk `{wentry.get('name')}` بدون metrics")
                continue
            member_oids = [str(m.get("oid", "")) for m in metrics]
            for m in metrics:
                oid = str(m.get("oid", ""))
                if oid in got_oids:
                    problems.append(f"{mod}: {oid} هم در get و هم در walk است")
                got_oids[oid] = f"walk:{wentry.get('name')}"
                if m.get("ignore_values"):
                    if list(m["ignore_values"]) != SENTINELS:
                        problems.append(f"{mod}/{m.get('name')}: ignore_values باید {SENTINELS} باشد، "
                                        f"نه {m['ignore_values']}")
                    ignore_actual.add(f"{mod}/{m.get('name')}")
            subtree = str(wentry.get("subtree", ""))
            expect = lcp(member_oids)
            if subtree != expect:
                problems.append(f"{mod}: subtree `{subtree}` ≠ LCP سطرهایش `{expect}`")
            if subtree not in all_matrix_oids and len(member_oids) < 2:
                problems.append(f"{mod}: walk `{wentry.get('name')}` تنها یک سطر می‌پوشاند "
                                f"و خودش OIDِ مستقل است ⇒ باید به get برود")
            for lk in wentry.get("lookups") or []:
                label_oid = str(lk.get("label_oid", ""))
                if label_oid not in rows:
                    problems.append(f"{mod}: lookup `{label_oid}` ردیفِ ماتریس این ماژول نیست")
        missing = set(rows) - set(got_oids)
        extra = set(got_oids) - set(rows)
        if missing:
            problems.append(f"{mod}: {len(missing)} OIDِ ماتریس در generator نیست: "
                            f"{sorted(missing)[:4]}{' …' if len(missing) > 4 else ''}")
        if extra:
            problems.append(f"{mod}: {len(extra)} OID بی‌منبع: {sorted(extra)[:4]}")
        per_module[mod] = {"matrix": len(rows), "covered": len(set(got_oids) & set(rows)),
                           "walk_entries": len(cfg.get("walk") or []),
                           "get": sum(1 for v in got_oids.values() if v == "get")}
        used |= {o for o in got_oids if o in rows}
        metric_oids |= set(got_oids)
        # The expectation of ignore_values is calculated from the matrix (the source of the rule is the code)

    # 2) No live OIDs outside of matrix/subtree
    live: Set[str] = set()
    for path, val in _walk_strings({"modules": modules, "auths": gen.get("auths") or {}}):
        v = val.strip()
        if OID_RE.match(v):
            live.add(v)
    allowed = all_matrix_oids | {str(w.get("subtree", "")).strip()
                                 for cfg in modules.values() if isinstance(cfg, dict)
                                 for w in (cfg.get("walk") or [])}
    stray = sorted(live - allowed)
    if stray:
        problems.append(f"{len(stray)} OID زنده در generator بیرون از ماتریس است (بدون "
                        f"مصرف‌کننده): {stray[:4]}")

    # 3) Orphans: just a comment
    text = gen_path.read_text(encoding="utf-8")
    commented = set(PROBE_COMMENT_RE.findall(text))
    live_orphans = sorted(orphans & metric_oids)   # The walk subtree covers the orphan
    if live_orphans:
        problems.append(f"یتیم‌ها نباید متریک زنده شوند: {live_orphans}")
    missing_comments = sorted(orphans - commented)
    if missing_comments:
        problems.append(f"{len(missing_comments)} یتیم در کامنت‌های probe-only نیست: "
                        f"{missing_comments[:4]}")

    # 4) auths
    auths = gen.get("auths") or {}
    if set(auths) != {"public_v1", "public_v2", "secure_v3"}:
        problems.append(f"auths باید دقیقاً public_v1/public_v2/secure_v3 باشد، هست: {sorted(auths)}")
    for name in ("public_v1", "public_v2"):
        a = auths.get(name) or {}
        if a.get("community") != "public" or not isinstance(a.get("version"), int):
            problems.append(f"auth `{name}` باید community=public و version عددی داشته باشد")
    v3 = auths.get("secure_v3") or {}
    if v3.get("version") != 3:
        problems.append("secure_v3 باید version: 3 داشته باشد")
    for key in ("username", "auth_passphrase", "priv_passphrase"):
        val = str(v3.get(key, ""))
        if not ENV_PLACEHOLDER_RE.match(val):
            problems.append(f"secure_v3.{key} باید placeholder محیطی باشد (`${{VAR}}`)، هست: {val!r}")

    # 5) matching `source:` with the document
    for mod, cfg in modules.items():
        if not isinstance(cfg, dict):
            continue
        rows = matrix.get(mod, {})
        entries = list(cfg.get("get") or []) + [m for w in (cfg.get("walk") or [])
                                                for m in (w.get("metrics") or [])]
        for e in entries:
            oid = str(e.get("oid", ""))
            row = rows.get(oid)
            src = str(e.get("source", "")).replace("\\", "/")
            if not row:
                continue
            want_src = f"{row['file']}:{row['line']}".replace("\\", "/")
            if src != want_src:
                problems.append(f"{mod}/{e.get('name')}: source `{src}` ≠ ماتریس `{want_src}`")

    # 6) ignore_values — the rule must be followed exactly
    expected_ignore = set()
    for mod, rows in matrix.items():
        cfg = modules.get(mod) or {}
        entries = {str(e.get("oid")): e for e in (cfg.get("get") or [])}
        for w in (cfg.get("walk") or []):
            for m in (w.get("metrics") or []):
                entries.setdefault(str(m.get("oid")), m)
        for oid, row in rows.items():
            e = entries.get(oid)
            if not e:
                continue
            name = str(e.get("name", ""))
            if needs_ignore(row, name):
                expected_ignore.add(f"{mod}/{name}")
    if expected_ignore - ignore_actual:
        problems.append(f"ignore_values روی این‌ها نیست: {sorted(expected_ignore - ignore_actual)[:5]}")
    if ignore_actual - expected_ignore:
        problems.append(f"ignore_values بی‌دلیل روی این‌ها گذاشته شده: "
                        f"{sorted(ignore_actual - expected_ignore)[:5]}")

    stats = {
        "modules": len(got & want),
        "matrix_oids": len(all_matrix_oids),
        "covered_oids": len(used),
        "per_module": per_module,
        "orphans_in_doc": len(orphans),
        "orphans_commented": len(commented & orphans),
        "live_stray_oids": stray,
        "ignore_values_expected": len(expected_ignore),
        "ignore_values_actual": len(ignore_actual),
        "auths": sorted(auths),
    }
    return problems, stats


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point of the OID coverage gate."""

    ap = argparse.ArgumentParser(description="گیت پوشش generator.yml در برابر docs/oid-matrix.md")
    ap.add_argument("--matrix", default=str(MATRIX_DOC))
    ap.add_argument("--generator", default=str(GENERATOR))
    ap.add_argument("--json", action="store_true", help="خروجی JSON (برای CI)")
    args = ap.parse_args(argv)

    problems, stats = check(Path(args.matrix), Path(args.generator))
    if not problems and not stats:
        return EXIT_MISSING
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems, "stats": stats},
                         ensure_ascii=False, indent=2))
    else:
        if problems:
            print("✘ پوشش OID کامل نیست:", file=sys.stderr)
            for p in problems:
                print(f"   • {p}", file=sys.stderr)
        else:
            s = stats
            print(f"✔ generator.yml ⊡ docs/oid-matrix.md — {s['covered_oids']}/{s['matrix_oids']} "
                  f"OID ماتریس پوشیده شد (ماژول: {s['modules']})")
            for mod, v in sorted(s["per_module"].items()):
                print(f"   {mod:18s} matrix={v['matrix']:3d} covered={v['covered']:3d} "
                      f"get={v['get']:3d} walk={v['walk_entries']:2d}")
            print(f"   یتیم‌ها: {s['orphans_commented']}/{s['orphans_in_doc']} در کامنت · "
                  f"OID زندهٔ بیرونِ ماتریس: {len(s['live_stray_oids'])} · "
                  f"ignore_values: {s['ignore_values_actual']}/{s['ignore_values_expected']}")
    return EXIT_FAIL if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
