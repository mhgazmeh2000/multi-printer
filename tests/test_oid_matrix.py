# -*- coding: utf-8 -*-
"""
تست‌های ماتریس OID (پیش‌نیاز G.1) — سه چیز را قفل می‌کند:

۱. سند `docs/oid-matrix.md` **کهنه نباشد** (همان چیزی که ابزار از کد می‌سازد).
۲. ابزار **قطعی** باشد: دو اجرای پیاپی، خروجی بایت‌به‌بایت یکی.
۳. **هیچ OIDی جعلی نباشد**: هر ردیف سند باید در فایل:خط اعلام‌شده واقعاً پیدا شود
   (یا به‌صورت «دُمِ» یک helper مثل `gc("…")` که با BASE ماژول کامل می‌شود).
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
    return subprocess.run([sys.executable, str(TOOL), *args], cwd=ROOT,
                          capture_output=True, text=True)


def _json(tmp_path: Path) -> dict:
    out = tmp_path / "oid.json"
    proc = _run("--json", str(out))
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_documented_matrix_is_in_sync_with_code():
    """گیت CI: اگر کسی OIDی را در کد عوض کند و سند را بازتولید نکند، این تست می‌شکند."""
    proc = _run("--check")
    assert proc.returncode == 0, (
        "docs/oid-matrix.md با کد نمی‌خواند؛ اجرا: python tools/build_oid_matrix.py --write\n"
        + proc.stdout + proc.stderr)


def test_tool_is_deterministic(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    for dest in (a, b):
        proc = _run("--write")
        assert proc.returncode == 0, proc.stderr
        dest.write_text(DOC.read_text(encoding="utf-8"), encoding="utf-8")
    assert a.read_bytes() == b.read_bytes(), "خروجی ابزار قطعی نیست"
    # و عدد سرجمعِ سند هم باید پایدار باشد
    assert re.search(r"\*\*\d+\*\* OID یکتا در ماتریس", a.read_text(encoding="utf-8"))


def test_no_invented_oid_every_row_resolves_in_code(tmp_path):
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
            # OID با چسبیدن دُمِ نوشته‌شده به یک ثابت ساخته شده (OID_BASE / BASE) ⇒
            # هم ثابت در فایل باشد، هم دُم، و اتصالشان دقیقاً همان OID باشد
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
    """تعداد ردیف‌های §۳ سند = شمارش ابزار (سندِ دستی‌شده از کد جدا نمی‌چرخد)."""
    data = _json(tmp_path)
    proc = _run("--write")                       # سند همیشه از کد ساخته می‌شود
    assert proc.returncode == 0, proc.stderr
    text = DOC.read_text(encoding="utf-8")
    doc_oids = {m.group(1) for m in ROW_RE.finditer(text)}
    code_oids = {r["oid"] for rs in data["modules"].values() for r in rs}
    assert doc_oids == code_oids, (
        f"اختلاف سند/کد: تنها-سند={sorted(doc_oids - code_oids)[:3]} "
        f"تنها-کد={sorted(code_oids - doc_oids)[:3]}")


def test_every_module_has_a_shape(tmp_path):
    """هر ماژول باید حداقل یک OID داشته باشد و هر OID پیشوند/برند درست."""
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
