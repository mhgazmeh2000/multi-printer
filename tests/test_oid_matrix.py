# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_oid_matrix.py
# @brief:        Tests for the OID matrix: docs in sync with code, deterministic output and no
#                invented OIDs.
# @details:      Responsibilities: Check the generated matrix and doc rows.
#                Main API: 5 test functions
#                Side effects: runs external processes, file I/O
# @dependencies: stdlib only
# @notes:        None.
# =============================================================================

"""
OID matrix tests (G.1 prerequisite) — Locks three things:

1. The `docs/oid-matrix.md` document is **not stale** (the same as what the tool builds from the code).
2. The tool is **definite**: two consecutive executions, one byte-by-byte output.
3. **No bogus OIDs**: Each document row must actually be found in the declared file:line
   (or as a "tail" of a helper such as `gc("...")" which is completed with BASE module).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "build_oid_matrix.py"
DOC = ROOT / "docs" / "oid-matrix.md"

ROW_RE = re.compile(r"^\|\s*`(\d+(?:\.\d+){6,})`\s*\|\s*(?:get|walk)\s*\|", re.M)


def _run(*args: str) -> subprocess.CompletedProcess:
    """Helper: run."""
    return subprocess.run([sys.executable, str(TOOL), *args], cwd=ROOT,
                          capture_output=True, text=True)


def _json(tmp_path: Path) -> dict:
    """Helper: json."""
    out = tmp_path / "oid.json"
    proc = _run("--json", str(out))
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_documented_matrix_is_in_sync_with_code():
    """CI Gate: This test breaks if someone changes an OID in the code and doesn't reproduce the document."""
    proc = _run("--check")
    assert proc.returncode == 0, (
        "docs/oid-matrix.md با کد نمی‌خواند؛ اجرا: python tools/build_oid_matrix.py --write\n"
        + proc.stdout + proc.stderr)


def test_tool_is_deterministic(tmp_path):
    """Verifies tool is deterministic."""
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    for dest in (a, b):
        proc = _run("--write")
        assert proc.returncode == 0, proc.stderr
        dest.write_text(DOC.read_text(encoding="utf-8"), encoding="utf-8")
    assert a.read_bytes() == b.read_bytes(), "خروجی ابزار قطعی نیست"
    # And the total number of the document must be stable
    assert re.search(r"\*\*\d+\*\* OID یکتا در ماتریس", a.read_text(encoding="utf-8"))


def test_no_invented_oid_every_row_resolves_in_code(tmp_path):
    """Verifies no invented oid every row resolves in code."""
    data = _json(tmp_path)
    modules = data["modules"]
    rows = [(m, r) for m, rs in modules.items() for r in rs]
    assert rows, "ماتریس خالی است — یعنی ابزار منبعی پیدا نکرده"
    checked = 0
    for mod, r in rows:
        src = ROOT / r["file"]
        assert src.is_file(), f"منبع ناموجود: {r['file']}"
        text = src.read_text(encoding="utf-8")
        line = text.splitlines()[int(r["line"]) - 1]
        head = r.get("head") or ""
        if head:
            # OID is created by sticking the written tail to a constant (OID_BASE / BASE) ⇒
            # both constant in the file, and the tail, and their connection must be exactly the same OID
            tail = r["oid"][len(head):].lstrip(".")
            assert head in text, f"{mod}: ثابت {head} در {r['file']} نیست"
            assert tail and tail in text, f"{mod}: دُم {tail} در {r['file']} نیست"
            assert r["oid"] == f"{head}.{tail}", f"{mod}: سازندگی {r['oid']} نمی‌بندد"
        else:
            assert r["oid"] in line, (
                f"{mod}: OID {r['oid']} عیناً در {r['file']}:{r['line']} نیست → {line!r}")
        checked += 1
    assert checked == data["stats"]["unique_oids"], "شمارِ ردیف‌ها با سرجمع نمی‌خواند"


def test_doc_rows_match_generated_doc(tmp_path):
    """§3 number of rows of the document = tool count (the manual document does not rotate separately from the code)."""
    data = _json(tmp_path)
    proc = _run("--write")                       # The document is always built from code
    assert proc.returncode == 0, proc.stderr
    text = DOC.read_text(encoding="utf-8")
    doc_oids = {m.group(1) for m in ROW_RE.finditer(text)}
    code_oids = {r["oid"] for rs in data["modules"].values() for r in rs}
    assert doc_oids == code_oids, (
        f"اختلاف سند/کد: تنها-سند={sorted(doc_oids - code_oids)[:3]} "
        f"تنها-کد={sorted(code_oids - doc_oids)[:3]}")


def test_every_module_has_a_shape(tmp_path):
    """Each module must have at least one OID, and each OID must have the correct prefix/brand."""
    data = _json(tmp_path)
    expect_prefix = {
        "printer_hp": "1.3.6.1.4.1.11", "printer_canon": "1.3.6.1.4.1.1602",
        "printer_brother": "1.3.6.1.4.1.2435", "printer_toshiba": "1.3.6.1.4.1.1129",
        "sensor_ecs100g": "1.3.6.1.4.1.47206", "printer_std": "1.3.6.1.2.1",
    }
    for mod, rows in data["modules"].items():
        prefix = expect_prefix.get(mod)
        if prefix is None:
            continue
        assert rows, f"ماژول {mod} خالی است"
        bad = [r["oid"] for r in rows if not r["oid"].startswith(prefix + ".")]
        assert not bad, f"{mod}: این OIDها زیر پیشوند {prefix} نیستند: {bad[:5]}"
