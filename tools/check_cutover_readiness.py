#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/check_cutover_readiness.py — سنجش **آمادگی** cutover (فاز E.7.2).

چرا این ابزار هست؟
------------------
`docs/cutover-runbook.md` ده گامِ روز اجرا را با «گیت» تعریف می‌کند. نیمی از آن گیت‌ها
قابل ماشین‌سازی‌اند؛ این ابزار همان نیمه را در یک اجرا بررسی می‌کند و رنگی گزارش می‌دهد،
تا هیچ cutoverی با پیش‌نیازِ ناکامل شروع نشود. **این ابزار چیزی را تغییر نمی‌دهد** —
همه‌ی اتصال‌ها فقط‌خواندنی‌اند و تنها نوشتن، گزارش `--out` است (اگر خواسته شود).

نه سنجه (به همان ترتیبِ خواسته‌شده):
    ۱) پایتون ≥ ۳٫۱۰
    ۲) درایور `psycopg` نصب باشد
    ۳) سرویس PostgreSQL در دسترس باشد (pg_isready یا سوکت)
    ۴) DSN واقعاً کار کند (SELECT 1)
    ۵) `alembic head` روی PG = سرشاخه‌ی مخزن (امروز `0004`)
    ۶) `PRAGMA integrity_check` روی logs.db
    ۷) timestampها UTC-Z باشند (از راه `tools/analyze_old_events.py`)
    ۸) فضای دیسک برای بکاپ‌ها کافی باشد
    ۹) پورت PG (پیش‌فرض ۵۴۳۲) باز باشد

کد خروج:
    0 = آماده (هیچ blocker و هیچ هشداری نیست)
    1 = **مسدود**: حداقل یک سنجهٔ قرمز ⇒ cutover مجاز نیست
    2 = فقط هشدار (زرد) ⇒ اجرا با «اقدام انسانی» ثبت‌شده مجاز است
  با `--strict` هشدارها هم قرمز می‌شوند (خروجی ۱).

نشانی اتصال (به همین ترتیب اولویت):
    ۱) `--dsn`  ۲) متغیر محیطی `PG_DSN`  ۳) متغیرهای libpq (`PGHOST`/`PGPORT`/…)

نمونه:
    python tools/check_cutover_readiness.py
    python tools/check_cutover_readiness.py --db D:\\printguard\\logs.db --out D:\\pgbackup\\ready.json
    python tools/check_cutover_readiness.py --skip-pg          # روی ماشینِ بدون PG
    python tools/check_cutover_readiness.py --self-test        # بدون شبکه، فقط منطق داخلی
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_WARN = 2

MIN_PYTHON: Tuple[int, int] = (3, 10)
DEFAULT_PORT = 5432

#: چند برابر اندازهٔ `logs.db` باید فضای آزاد باشد (بکاپ SQLite + dump PG + دیتای PG).
DISK_FACTOR_BLOCK = 2.0
DISK_FACTOR_WARN = 4.0

STATUS_ORDER = ("ok", "skip", "warn", "fail")
MARK = {"ok": "✔", "skip": "–", "warn": "!", "fail": "✘"}


# ─────────────────────────────── خروجی رنگی ───────────────────────────────
def _color_enabled(force: Optional[bool]) -> bool:
    """رنگ فقط وقتی روشن می‌شود که ترمینال آن را پشتیبانی کند (در پایپ/CI خاموش)."""
    if force is not None:
        return force
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("PRINTGUARD_FORCE_COLOR") == "1":
        return True
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def paint(text: str, status: str, enabled: bool) -> str:
    if not enabled:
        return text
    codes = {"ok": "32", "warn": "33", "fail": "31", "skip": "90"}
    code = codes.get(status)
    return f"\033[{code}m{text}\033[0m" if code else text


class Result(dict):
    """یک ردیف چک‌لیست: `key`/`label`/`status`/`detail`/`action`."""

    def __init__(self, key: str, label: str, status: str, detail: str = "",
                 action: str = "") -> None:
        super().__init__(key=key, label=label, status=status, detail=detail, action=action)

    @property
    def status(self) -> str:
        return self["status"]


def ok(key: str, label: str, detail: str = "") -> Result:
    return Result(key, label, "ok", detail)


def warn(key: str, label: str, detail: str = "", action: str = "") -> Result:
    return Result(key, label, "warn", detail, action)


def fail(key: str, label: str, detail: str = "", action: str = "") -> Result:
    return Result(key, label, "fail", detail, action)


def skipped(key: str, label: str, detail: str = "") -> Result:
    return Result(key, label, "skip", detail)


# ─────────────────────────────── خواندن DSN ───────────────────────────────
_URL_RE = re.compile(
    r"^postgres(?:ql)?(?:\+\w+)?://(?:(?P<user>[^:@/]*)(?::(?P<password>[^@/]*))?@)?"
    r"(?P<host>[^:/?#]*)(?::(?P<port>\d+))?(?:/(?P<dbname>[^?#]*))?",
    re.I,
)


def parse_dsn(raw: Optional[str] = None) -> Dict[str, str]:
    """
    `PG_DSN` را می‌خواند و به قطعات libpq تبدیل می‌کند.

    سه قالب پذیرفته می‌شود (هر سه در این مخزن/مستندات واقعی‌اند):
      • SQLAlchemy URL:  `postgresql+psycopg://user:pw@host:5432/dbname`
      • conninfo:         `host=localhost port=5432 dbname=printguard …`
      • خالی:            متغیرهای استاندارد libpq + پیش‌فرض‌ها
    `options=…` (مثلاً `-csearch_path=…`) حذف نمی‌شود؛ برای اتصال لازم است.
    """
    value = (raw if raw is not None else os.environ.get("PG_DSN", "") or "").strip()
    out: Dict[str, str] = {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": os.environ.get("PGPORT", str(DEFAULT_PORT)),
        "dbname": os.environ.get("PGDATABASE", "printguard"),
        "user": os.environ.get("PGUSER", ""),
        "password": os.environ.get("PGPASSWORD", ""),
        "options": "",
        "source": "env-libpq" if value == "" else ("--dsn" if raw is not None else "PG_DSN"),
    }
    if not value:
        return out
    m = _URL_RE.match(value)
    if m:
        g = m.groupdict()
        if g.get("host"):
            out["host"] = g["host"]
        if g.get("port"):
            out["port"] = g["port"]
        if g.get("dbname"):
            out["dbname"] = g["dbname"]
        if g.get("user"):
            out["user"] = g["user"]
        if g.get("password"):
            out["password"] = g["password"]
        q = value.split("?", 1)
        if len(q) == 2:
            for part in q[1].split("&"):
                if part.lower().startswith("options="):
                    out["options"] = part.split("=", 1)[1].replace("%20", " ")
        return out
    # قالب conninfo: جفت‌های `key=value` (فاصله‌جداسازی‌شده)
    tokens = value.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if "=" in tok:
            key, _, val = tok.partition("=")
            key = key.strip().lower()
            if key in out and val:
                out[key] = val
            elif key in out:                       # `key=` خالی ⇒ مقدار در توکن بعدی است
                nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
                if nxt and "=" not in nxt:
                    out[key] = nxt
                    i += 1
        i += 1
    return out


def mask(dsn_cfg: Dict[str, str]) -> str:
    """نشانی را بدون رمز چاپ می‌کند (برای لاگ/گزارش)."""
    bits = [f"host={dsn_cfg.get('host', '')}", f"port={dsn_cfg.get('port', '')}",
            f"dbname={dsn_cfg.get('dbname', '')}"]
    if dsn_cfg.get("user"):
        bits.append(f"user={dsn_cfg['user']}")
    if dsn_cfg.get("password"):
        bits.append("password=***")
    if dsn_cfg.get("options"):
        bits.append(f"options={dsn_cfg['options']}")
    return " ".join(bits)


def connect_kwargs(dsn_cfg: Dict[str, str], timeout: int = 5) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "host": dsn_cfg.get("host") or "localhost",
        "port": int(dsn_cfg.get("port") or DEFAULT_PORT),
        "dbname": dsn_cfg.get("dbname") or "printguard",
        "connect_timeout": timeout,
    }
    if dsn_cfg.get("user"):
        kwargs["user"] = dsn_cfg["user"]
    if dsn_cfg.get("password"):
        kwargs["password"] = dsn_cfg["password"]
    if dsn_cfg.get("options"):
        kwargs["options"] = dsn_cfg["options"]
    return kwargs


# ─────────────────────────────── سرشاخهٔ alembic ───────────────────────────────
_REV_RE = re.compile(r"""^\s*revision(?::\s*str)?\s*=\s*['"]([^'"]+)['"]""", re.M)
_DOWN_RE = re.compile(r"""^\s*down_revision(?::\s*str\?)?\s*=\s*['"]?([^'"\n]+?)['"]?\s*$""",
                      re.M)


def discover_heads(versions_dir: Path) -> List[str]:
    """
    سرشاخهٔ migrationها را از فایل‌های `alembic/versions/*.py` بیرون می‌کشد.

    عدد `0004` در این ابزار **hardcode نشده** است: اگر فاز F یک migration اضافه کند،
    سنجهٔ ۵ خودبه‌خود همان را انتظار می‌کشد. اگر دو سرشاخه باشد، لیست دو عضو می‌شود و
    سنجه قرمز است (merge لازم است).
    """
    if not versions_dir.is_dir():
        return []
    revisions: Dict[str, Optional[str]] = {}
    for path in sorted(versions_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        m = _REV_RE.search(text)
        if not m:
            continue
        rev = m.group(1).strip()
        d = _DOWN_RE.search(text)
        down = d.group(1).strip() if d and d.group(1).strip() not in ("None", "''", '""') else None
        revisions[rev] = down
    if not revisions:
        return []
    referenced = {d for d in revisions.values() if d}
    return sorted(r for r in revisions if r not in referenced)


# ─────────────────────────────── سنجه‌ها ───────────────────────────────
def check_python(version: Optional[Tuple[int, ...]] = None) -> Result:
    v = tuple(version or sys.version_info[:3])
    label = "پایتون ≥ ۳٫۱۰"
    got = ".".join(str(x) for x in v[:3])
    if v[:2] < MIN_PYTHON:
        return fail("python", label, f"نسخه {got} (< 3.10)",
                    "Python 3.10+ نصب کنید و همان را در `.env`/PATH اول بگذارید.")
    return ok("python", label, got)


def check_psycopg() -> Result:
    label = "درایور psycopg (نسخه ۳)"
    spec = importlib.util.find_spec("psycopg")
    if spec is None:
        return fail("psycopg", label, "import psycopg ناموفق بود",
                    "pip install \"psycopg[binary]\"  (در همان venv که برنامه اجرا می‌شود)")
    try:
        import psycopg  # noqa: WPS433 — بررسی نسخه، نه استفاده
        ver = getattr(psycopg, "__version__", "?")
    except Exception as exc:  # noqa: BLE001 — خرابی import هم قرمز است
        return fail("psycopg", label, f"import خطا داد: {exc}", "reinstall psycopg")
    if str(ver).startswith("2"):
        return fail("psycopg", label, f"psycopg2 تشخیص داده شد ({ver})",
                    "این مخزن درایور ۳ می‌خواهد: pip install \"psycopg[binary]\"")
    return ok("psycopg", label, str(ver))


def check_service(dsn_cfg: Dict[str, str]) -> Result:
    """سنجهٔ ۳: سرویس PG بالا باشد — `pg_isready` اگر در PATH باشد، وگرنه سوکت."""
    label = "سرویس PostgreSQL پاسخ می‌دهد"
    host, port = dsn_cfg.get("host") or "localhost", int(dsn_cfg.get("port") or DEFAULT_PORT)
    exe = shutil.which("pg_isready")
    if exe:
        cmd = [exe, "-h", host, "-p", str(port), "-t", "5"]
        if dsn_cfg.get("dbname"):
            cmd += ["-d", dsn_cfg["dbname"]]
        if dsn_cfg.get("user"):
            cmd += ["-U", dsn_cfg["user"]]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=15)
            msg = (proc.stdout or proc.stderr).strip().splitlines()
            detail = msg[-1][:160] if msg else f"exit={proc.returncode}"
            if proc.returncode == 0:
                return ok("service", label, detail)
            return fail("service", label, detail,
                        "سرویس را روشن کنید (services.msc → postgresql-x64-16) یا `pg_isready` را "
                        "دستی اجرا کنید")
        except Exception as exc:  # noqa: BLE001
            return warn("service", label, f"pg_isready اجرا نشد ({exc})؛ سوکت بررسی می‌شود", "")
    try:
        with socket.create_connection((host, port), timeout=5):
            return ok("service", label, f"سوکت {host}:{port} باز است (pg_isready در PATH نیست)")
    except OSError as exc:
        return fail("service", label, f"سوکت {host}:{port} برقرار نشد: {exc}",
                    "سرویس PostgreSQL را روشن کنید یا host/port را در PG_DSN درست کنید")


def _default_connect(dsn_cfg: Dict[str, str], timeout: int):
    import psycopg  # تنها جایی که به درایور نیاز داریم

    return psycopg.connect(**connect_kwargs(dsn_cfg, timeout))


def check_dsn(dsn_cfg: Dict[str, str], connect: Optional[Callable] = None,
              timeout: int = 5) -> Result:
    """سنجهٔ ۴: اتصال واقعی + `SELECT 1`."""
    label = "DSN کار می‌کند (SELECT 1)"
    conn = connect or _default_connect
    try:
        with conn(dsn_cfg, timeout) as c:
            cur = c.cursor()
            cur.execute("SELECT 1")
            one = cur.fetchone()[0]
            cur.execute("SELECT current_database(), version()")
            dbname, ver = cur.fetchone()
        if one != 1:
            return fail("dsn", label, f"SELECT 1 ⇒ {one!r}", "اتصال را بررسی کنید")
        return ok("dsn", label, f"{mask(dsn_cfg)} | db={dbname} | {str(ver).split(',')[0]}")
    except Exception as exc:  # noqa: BLE001 — هر خطای اتصال یعنی آماده نیستیم
        return fail("dsn", label, f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:170]}",
                    "PG_DSN را با `psql` دستی تست کنید؛ سپس این ابزار را دوباره اجرا کنید")


def check_alembic_head(dsn_cfg: Dict[str, str], connect: Optional[Callable] = None,
                       versions_dir: Optional[Path] = None, timeout: int = 5) -> Result:
    """سنجهٔ ۵: `alembic_version` روی PG = سرشاخهٔ مخزن (schema-aware، بدون hardcode)."""
    label = "alembic head روی PG"
    heads = discover_heads(versions_dir or (PROJECT_ROOT / "alembic" / "versions"))
    if not heads:
        return fail("head", label, "سرشاخه‌ای در alembic/versions پیدا نشد",
                    "از ریشهٔ مخزن اجرا کنید (ابزار نسبت به مسیر فایل خودش جست‌وجو می‌کند)")
    want = ", ".join(heads)
    conn = connect or _default_connect
    try:
        with conn(dsn_cfg, timeout) as c:
            cur = c.cursor()
            # بدون نام‌گذاری schema: search_path (شامل options=-csearch_path=…) رعایت شود
            cur.execute("SELECT current_schema(), current_setting('search_path', true)")
            cschema, spath = cur.fetchone()
            try:
                cur.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
                got = [r[0] for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                cur.execute("SELECT version_num FROM public.alembic_version ORDER BY version_num")
                got = [r[0] for r in cur.fetchall()]
                cschema = "public (fallback)"
        if not got:
            return fail("head", label, "جدول alembic_version هیچ سطری ندارد",
                        "python -m alembic upgrade head (با DB_BACKEND=postgresql)")
        if got == heads:
            return ok("head", label, f"version_num={got[0]} | schema={cschema} | search_path={spath}")
        return fail("head", label, f"روی PG: {', '.join(got)} — انتظار مخزن: {want}",
                    f"python -m alembic upgrade head ⇒ {want}")
    except Exception as exc:  # noqa: BLE001
        return fail("head", label, f"خوانده نشد ({type(exc).__name__}: "
                    f"{str(exc).strip().splitlines()[0][:140]})",
                    f"انتظار: {want}. اگر دیتابیس تازه است، alembic upgrade head اجرا نشده")


def check_integrity(db_path: Path) -> Result:
    """سنجهٔ ۶: `PRAGMA integrity_check` + شمارش خلاصه (فقط‌خواندنی)."""
    label = "سلامت فایل logs.db"
    if not db_path.is_file():
        return fail("integrity", label, f"یافت نشد: {db_path}",
                    "از ریشهٔ پروژه اجرا کنید یا --db را بدهید")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as exc:
        return fail("integrity", label, f"باز نشد: {exc}", "اجازهٔ خواندن فایل را بررسی کنید")
    try:
        verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
        rows = conn.execute("SELECT count(*) FROM logs").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE ? ORDER BY name",
            ("table", "sqlite_%"))]
        size = db_path.stat().st_size
        if str(verdict).lower() != "ok":
            return fail("integrity", label, f"integrity_check ⇒ {verdict}",
                        "قبل از هر کاری از بکاپِ دیسک دوم restore کنید و cutover را متوقف کنید")
        detail = (f"integrity=ok | logs={rows} ردیف | {len(tables)} جدول "
                  f"(۱۱ برنامه + alembic_version) | {size:,} بایت")
        if len(tables) < 11:
            return warn("integrity", label, detail,
                        "جدول‌ها کمتر از ۱۱ جدول برنامه است؛ migration کامل نشده؟")
        return ok("integrity", label, detail)
    except sqlite3.DatabaseError as exc:
        return fail("integrity", label, f"فایل دیتابیس معتبر نیست: {exc}", "بکاپ → restore")
    finally:
        conn.close()


def check_timestamps(db_path: Path, analyze_tool: Optional[Path] = None,
                      cutover: str = "") -> Result:
    """سنجهٔ ۷: قالب timestampها — با فراخوانی همان `analyze_old_events.py` (کد خروجش مرجع است)."""
    label = "timestampها UTC-Z و بازه‌ی pre/post cutover"
    tool = analyze_tool or (TOOLS_DIR / "analyze_old_events.py")
    if not tool.is_file():
        return skipped("timestamps", label, f"ابزار یافت نشد: {tool}")
    cmd = [sys.executable, str(tool), "--db", str(db_path)]
    if cutover:
        cmd += ["--cutover", cutover]
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as fh:
            tmp = Path(fh.name)
        cmd += ["--json", str(tmp)]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180,
                              cwd=str(PROJECT_ROOT))  # زمان کامیتِ cutover از git همین مخزن
        data: Dict[str, Any] = {}
        if tmp.is_file():
            try:
                data = json.loads(tmp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
        tables = data.get("tables") or []
        counts = {t.get("table"): t for t in tables if isinstance(t, dict)}
        flagged = sum(sum(v for k, v in (t.get("by_class") or {}).items()
                          if k in ("naive_local", "invalid", "z_cutover_unknown",
                                   "z_before_cutover")) for t in tables if isinstance(t, dict))
        tail = (proc.stdout or proc.stderr).strip().splitlines()
        summary = next((ln for ln in reversed(tail)
                        if re.match(r"^\[(WARN|ERROR|FAIL)\]", ln.strip())), "")
        head = data.get("cutover") or "?"
        per_table = ", ".join("%s=%s" % (k, v.get("total")) for k, v in counts.items()) or "—"
        detail = (f"cutover≈{head} | ردیف‌های نیازمند بازبینی={flagged} | "
                  f"per-table={per_table}")
        if proc.returncode == 0:
            return ok("timestamps", label, detail)
        if proc.returncode == 2:
            return warn("timestamps", label, detail + (f" | {summary[:120]}" if summary else ""),
                        "تصمیم این ردیف‌ها در فاز F.5 است؛ در لاگ اجرا ثبتشان کنید "
                        "(runbook §۲.۵). cutover را متوقف نمی‌کند.")
        return fail("timestamps", label, f"ابزار خطا داد (exit={proc.returncode}): "
                    f"{(tail[-1] if tail else '')[:150]}", "ابتدا خودِ ابزار را دستی اجرا کنید")
    except Exception as exc:  # noqa: BLE001
        return warn("timestamps", label, f"اجرای ابزار ممکن نشد: {type(exc).__name__}: {exc}",
                    "دستی اجرا کنید: python tools/analyze_old_events.py --db logs.db")
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def check_disk(db_path: Path, extra_bytes: int = 0) -> Result:
    """سنجهٔ ۸: فضای آزاد همان درایو، نسبت به اندازهٔ logs.db (قانون runbook: ۴ برابر)."""
    label = "فضای دیسک برای بکاپ‌ها"
    base = db_path.parent if db_path.parent.exists() else Path.cwd()
    try:
        size = db_path.stat().st_size if db_path.is_file() else 0
        usage = shutil.disk_usage(base)
    except OSError as exc:
        return warn("disk", label, f"خوانده نشد: {exc}", "دستی بررسی کنید (This PC → Properties)")
    free = int(usage.free)
    need = int(size * DISK_FACTOR_WARN) + int(extra_bytes)
    ratio = (free / size) if size else 0.0
    shown = f"{ratio:.0f}×" if ratio >= 100 else f"{ratio:.1f}×"
    detail = f"{base} | آزاد={free / 1e9:.2f} GB | logs.db={size:,} B | نسبت={shown}"
    if size and free < size * DISK_FACTOR_BLOCK:
        return fail("disk", label, detail,
                    "حداقل ۲ برابر logs.db فضای آزاد لازم است (بکاپ + dump)؛ دیسک را خالی کنید")
    if size and free < need:
        return warn("disk", label, f"{detail} | قانون runbook: ≥ {DISK_FACTOR_WARN:.0f}×",
                    "برای بکاپ‌های روزِ اجرا و دیتای PG فضای ۴ برابری کنار بگذارید")
    return ok("disk", label, detail)


def check_port(dsn_cfg: Dict[str, str], timeout: int = 3) -> Result:
    """سنجهٔ ۹: پورت از راه TCP باز باشد (firewall/bind را می‌گیرد)."""
    host, port = dsn_cfg.get("host") or "localhost", int(dsn_cfg.get("port") or DEFAULT_PORT)
    label = f"پورت {port} باز است"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ok("port", label, f"TCP connect به {host}:{port} موفق")
    except OSError as exc:
        return fail("port", label, f"{host}:{port} ⇒ {exc}",
                    "listen_addresses/pg_hba.conf/firewall را بررسی کنید (postgresql-setup.md §۲)")


def check_marker(db_path: Path) -> Result:
    """اطلاعات کمکی (بدون نمره): marker cutover نوشته شده یا نه."""
    label = "marker cutover (وضعیت فعلی)"
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='cutover_utc'").fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
    except sqlite3.Error:
        return skipped("marker", label, "خوانده نشد")
    if row:
        return warn("marker", label, f"نوشته شده: {row[0]}",
                    "برای cutoverِ تازه باید marker پیشین با git revert برداشته شود (runbook §۵)")
    return ok("marker", label, "نوشته نشده (وضعیت درست پیش از روز اجرا)")


# ─────────────────────────────── گزارش ───────────────────────────────
def exit_code(results: List[Result], strict: bool = False) -> int:
    statuses = [r.status for r in results]
    if "fail" in statuses:
        return EXIT_BLOCKED
    if "warn" in statuses:
        return EXIT_BLOCKED if strict else EXIT_WARN
    return EXIT_OK


def render(results: List[Result], color: bool, title: str, strict: bool = False) -> str:
    width = max((len(r["label"]) for r in results), default=24)
    lines = ["─" * 72, paint(title, "ok", color), "─" * 72]
    for r in results:
        mark = paint(f"[{MARK[r.status]}]", r.status, color)
        label = r["label"].ljust(width)
        detail = r["detail"].replace("\n", " ") if r["detail"] else ""
        lines.append(f"{mark} {label}  {detail}")
        if r["action"]:
            lines.append("     ↳ " + paint(r["action"], "warn" if r.status == "warn" else "fail",
                                            color))
    lines.append("─" * 72)
    counts = {s: sum(1 for r in results if r.status == s) for s in STATUS_ORDER}
    # حکم نهایی از همان exit_code می‌آید، تا «خطِ نتیجه» هیچ‌وقت با کد خروج یکی نباشد نه
    # (با --strict هشدارها قرمز شمرده می‌شوند).
    code = exit_code(results, strict=strict)
    verdict = {EXIT_OK: "آماده ✔", EXIT_WARN: "آماده با هشدار !", EXIT_BLOCKED: "مسدود ✘"}[code]
    tone = {EXIT_OK: "ok", EXIT_WARN: "warn", EXIT_BLOCKED: "fail"}[code]
    suffix = "  [--strict: هشدار = مسدود]" if (strict and counts["warn"] and not counts["fail"]) else ""
    lines.append(paint(f"نتیجه: {verdict}{suffix}   "
                       f"(✔ {counts['ok']} | ! {counts['warn']} | ✘ {counts['fail']} | "
                       f"– {counts['skip']})", tone, color))
    return "\n".join(lines)


def run_checks(db_path: Path, dsn_cfg: Dict[str, str], *, want_pg: bool = True,
               psycopg_ready: bool = True, connect: Optional[Callable] = None,
               cutover: str = "", versions_dir: Optional[Path] = None) -> List[Result]:
    """ترتیب اجرا = ترتیب گزارش (همان ۹ سنجه، بعد یک ردیف کمکی)."""
    out: List[Result] = [check_python()]
    out.append(check_psycopg())
    if not want_pg:
        for key, label in (("service", "سرویس PostgreSQL"), ("dsn", "DSN (SELECT 1)"),
                           ("head", "alembic head روی PG"), ("port", "پورت PG")):
            out.append(skipped(key, label + " (--skip-pg)"))
    else:
        out.append(check_service(dsn_cfg))
        out.append(check_dsn(dsn_cfg, connect=connect) if psycopg_ready
                   else fail("dsn", "DSN (SELECT 1)", "psycopg نصب نیست ⇒ اتصال تست نشد",
                             "pip install \"psycopg[binary]\""))
        out.append(check_alembic_head(dsn_cfg, connect=connect, versions_dir=versions_dir)
                   if psycopg_ready else
                   skipped("head", "alembic head روی PG", "بدون psycopg تست نمی‌شود"))
        out.append(check_port(dsn_cfg))
    out.append(check_integrity(db_path))
    out.append(check_timestamps(db_path, cutover=cutover))
    out.append(check_disk(db_path))
    out.append(check_marker(db_path))
    return out


def _load_dotenv_into_environ() -> None:
    """اگر `config.settings` در دسترس باشد، `.env` را وارد محیط می‌کند (بدون fallback بی‌صدا)."""
    if (PROJECT_ROOT / "config" / "settings.py").is_file() and str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    import warnings as _w

    try:
        # side-effect لازم: `config/settings.py` با `load_dotenv()` فایل `.env` را به env می‌آورد
        with _w.catch_warnings():
            _w.simplefilter("ignore", RuntimeWarning)  # هشدار SECRET_KEY اینجا بی‌ربط است
            importlib.import_module("config.settings")
    except Exception:  # noqa: BLE001 — ابزار نباید به‌خاطر config بمیرد
        pass


def _self_test() -> int:
    """منطق داخلی را بدون شبکه/دیتابیس واقعی می‌آزماید (خروجی: ۰ = سبز)."""
    assert parse_dsn("postgresql+psycopg://u:p@h:6432/db")["port"] == "6432"
    assert parse_dsn("postgresql+psycopg://u:p@h:6432/db")["password"] == "p"
    assert parse_dsn("host=127.0.0.1 port=5432 dbname=pg user=x")["dbname"] == "pg"
    assert "password=***" in mask(parse_dsn("postgresql://u:p@h/db"))
    heads = discover_heads(PROJECT_ROOT / "alembic" / "versions")
    assert heads and all(re.match(r"^\d{4}_", h) or h.isdigit() for h in heads), heads
    assert exit_code([ok("a", "a"), ok("b", "b")]) == EXIT_OK
    assert exit_code([ok("a", "a"), warn("b", "b")]) == EXIT_WARN
    assert exit_code([ok("a", "a"), warn("b", "b")], strict=True) == EXIT_BLOCKED
    assert exit_code([fail("a", "a"), warn("b", "b")]) == EXIT_BLOCKED
    assert check_python((3, 10, 0)).status == "ok"
    assert check_python((3, 9, 9)).status == "fail"
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "logs.db"
        conn = sqlite3.connect(good)
        conn.executescript("CREATE TABLE logs(id INTEGER PRIMARY KEY, timestamp TEXT);"
                           "INSERT INTO logs(timestamp) VALUES('2026-01-01T00:00:00Z');")
        conn.commit()
        conn.close()
        assert check_integrity(good).status in ("ok", "warn")
        bad = Path(td) / "bad.db"
        bad.write_bytes(b"not a database at all" * 40)
        assert check_integrity(bad).status == "fail"
    print("✔ self-test: منطق تجزیه/حکم/یکپارچگی درست کار می‌کند "
          f"(سرشاخهٔ مخزن: {', '.join(heads)})")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="check_cutover_readiness.py",
        description="چک‌لیست رنگی آمادگی cutover به PostgreSQL (فقط‌خواندنی).",
    )
    ap.add_argument("--db", default="logs.db", help="مسیر logs.db (پیش‌فرض: ./logs.db)")
    ap.add_argument("--dsn", default=None,
                    help="نشانی PG؛ پیش‌فرض از PG_DSN و سپس متغیرهای libpq")
    ap.add_argument("--cutover", default="",
                    help="تاریخ cutover (ISO UTC با Z) که به analyze_old_events داده می‌شود")
    ap.add_argument("--skip-pg", action="store_true",
                    help="سنجه‌های PG اجرا نشوند (ماشین بدون دیتابیس)")
    ap.add_argument("--strict", action="store_true", help="هشدار = مسدود (خروجی ۱)")
    ap.add_argument("--json", dest="json_path", default="",
                    help="گزارش ماشینی را در این فایل بنویس")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    ap.add_argument("--quiet", action="store_true", help="فقط خط نتیجه")
    ap.add_argument("--self-test", action="store_true",
                    help="بدون اتصال، فقط منطق داخلی را بسازد/بازبینی کند")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    _load_dotenv_into_environ()
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = (PROJECT_ROOT / args.db) if (PROJECT_ROOT / args.db).is_file() else db_path
    dsn_cfg = parse_dsn(args.dsn)
    force = {"always": True, "never": False}.get(args.color)
    color = _color_enabled(force)
    driver = importlib.util.find_spec("psycopg") is not None
    results = run_checks(db_path, dsn_cfg, want_pg=not args.skip_pg, psycopg_ready=driver,
                         cutover=args.cutover)
    code = exit_code(results, strict=args.strict)
    title = "چک‌لیست آمادگی cutover — PostgreSQL (E.7.2)"
    report = render(results, color, title, strict=bool(args.strict))
    if args.quiet:
        counts = {s: sum(1 for r in results if r.status == s) for s in STATUS_ORDER}
        report = (f"readiness: {code}  (ok={counts['ok']} warn={counts['warn']} "
                  f"fail={counts['fail']} skip={counts['skip']})")
    print(report)
    if args.json_path:
        payload = {
            "generated_at_utc": _utc_now_iso(),
            "db": str(db_path),
            "dsn": mask(dsn_cfg),
            "expected_heads": discover_heads(PROJECT_ROOT / "alembic" / "versions"),
            "exit_code": code,
            "strict": bool(args.strict),
            "checks": [dict(r) for r in results],
        }
        out_path = Path(args.json_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        if not args.quiet:
            print(f"🧾 گزارش ماشینی: {out_path}")
    if code == EXIT_BLOCKED:
        print("⛔ cutover مجاز نیست: ردیف‌های ✘ را برطرف کنید و دوباره اجرا کنید "
              "(runbook §۱ و §۲).")
    elif code == EXIT_WARN:
        print("⚠ آماده با هشدار: ردیف‌های ! را در لاگ اجرا ثبت کنید (runbook §۰).")
    else:
        print("✅ آماده — هیچ مسدودی نیست.")
    return code


def _utc_now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    sys.exit(main())
