#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/export_from_pg.py
# @brief:        Exports PostgreSQL data back to SQLite (return path after cutover).
# @details:      Responsibilities: Convert values and DDL for SQLite and copy the tables.
#                Main API: sqlite_type(), to_sqlite_value(), sqlite_ddl(), quote_ident(),
#                sql_literal(), load_driver(), dsn_from_env(), mask_dsn() (+7 more)
#                Side effects: database access, file I/O
# @dependencies: external: psycopg, psycopg2
# @notes:        Reads environment variables: PGPASSWORD, PG_DSN. Can be run directly as a script.
# =============================================================================

"""
tools/export_from_pg.py — export PostgreSQL → SQLite (return path after cutover).

Why is this tool necessary?
-----------------------
According to `docs/cutover-plan.md`, cutover moment = **first Flask write on PostgreSQL**.
After that, `DB_BACKEND=sqlite` is no longer a fallback path (fresh data is only in PG).
For a real rollback we must first roll back the PG to a healthy SQLite file.

This tool:
  1) reads tables from `information_schema` (no hard coded names);
  2) for each column it infers the SQLite type from the PG type;
  3) translates values ​​into SQLite equivalent (bool→0/1, TIMESTAMPTZ→ISO, JSONB→JSON text);
  4) Either it creates a complete `.db` file (`--mode db`) or only prints `INSERTs`
     (`--mode sql`, to insert into the database created with the application itself);
  5) Output monitor (`--manifest`) with line count + sha256 + count
     Writes "naive timestamps" (evidence for phase E's UTC-aware requirement).

Phase E prerequisite: installing ``psycopg'' (version 3) or ``psycopg2''. Today (Phase E.0) backend
`postgresql` is not enabled in `core/db`; This tool is ready for implementation in phase E and
Its full path can only be tested with a real PG. To test the logic of translation without PG:

    python tools/export_from_pg.py --self-test

Connection address (in order of priority):
    1) `--dsn`
    2) environment variable `PG_DSN`
    3) libpq standard variables (`PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD`)

Exit code:
    0 = successful
    1 = error (connect/write/translate)
    2 = Human action required: psycopg not installed, DSN not found, or destination exists

Example:
    python tools/export_from_pg.py --out archives/logs-after-cutover.db --manifest
    python tools/export_from_pg.py --mode sql --tables logs,users > restore.sql
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_ACTION = 2

#: Suggested ordering of tables (independent tables first) — just for better messaging;
#: The script itself accepts any order given by PG.
KNOWN_TABLES = (
    "logs",
    "toner_history",
    "sensor_readings",
    "users",
    "printer_counters",
    "cartridge_state",
    "cartridge_yield_profiles",
    "yield_samples",
    "toner_snapshots_v2",
    "security_events",
)


# ─────────────────────────────────────────────────────────────
# Type and value translation (pure functions - these are tested in --self-test)
# ─────────────────────────────────────────────────────────────

def sqlite_type(pg_type: str, udt_name: str = "") -> str:
    """Maps the PG type to the closest SQLite type.

    The map is compatible with `docs/postgresql-strictness.md` §4:
      • boolean → INTEGER (application works with 0/1)
      • timestamp*/date/time → TEXT (ISO format; like SQLite today)
      • json/jsonb → TEXT (string JSON; like columns details/allowed_*)
      • numeric/decimal/real/double precision → REAL
    """
    t = (pg_type or "").strip().lower()
    u = (udt_name or "").strip().lower()
    if t == "boolean" or u == "bool":
        return "INTEGER"
    if t in ("smallint", "integer", "bigint") or u in ("int2", "int4", "int8"):
        return "INTEGER"
    if t in ("numeric", "decimal", "real", "double precision") or u in ("float4", "float8", "numeric"):
        return "REAL"
    if t in ("timestamp with time zone", "timestamp without time zone", "timestamp",
             "date", "time with time zone", "time without time zone", "time") \
            or u in ("timestamptz", "timestamp", "date", "time", "timetz"):
        return "TEXT"
    if t in ("json", "jsonb") or u in ("json", "jsonb"):
        return "TEXT"
    if t == "bytea" or u == "bytea":
        return "BLOB"
    return "TEXT"


def to_sqlite_value(value, sqlite_col_type: str):
    """Converts the PG value to a value accepted by `sqlite3`.

    Second output: True if the value of a timestamp was **naive** (must be zero; witness
    for "all UTC-aware writers").
    """
    naive = False
    if value is None:
        return None, naive

    if isinstance(value, bool):
        return int(value), naive

    if isinstance(value, _dt.datetime):
        # The "T" separator and the microseconds are preserved so that the output is **lossless**;
        # The aware value is transferred to UTC so that no ambiguity remains in the TZ upon return.
        if value.tzinfo is None:
            naive = True
            return value.isoformat(timespec="microseconds"), naive
        return value.astimezone(_dt.timezone.utc).isoformat(timespec="microseconds"), naive

    if isinstance(value, _dt.date):
        return value.isoformat(), naive

    if isinstance(value, _dt.time):
        return value.isoformat(), naive

    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=False), naive

    if isinstance(value, bytes):
        return value, naive

    if isinstance(value, float):
        return value, naive

    try:  # Decimal and special numeric types
        num = float(value)
        if num.is_integer() and not isinstance(value, float) and sqlite_col_type == "INTEGER":
            return int(num), naive
        return num, naive
    except (TypeError, ValueError):
        pass

    return str(value), naive


def sqlite_ddl(table: str, columns) -> str:
    """DDL creates the SQLite table from the metadata of the PG columns.

    `columns` = [(name, sqlite_type, is_pk), ...]
    Multi-column primary key is supported (`PRIMARY KEY (a, b)`).
    """
    pk = [c[0] for c in columns if c[2]]
    parts = []
    for name, col_type, is_pk in columns:
        piece = f'    "{name}" {col_type}'
        if is_pk and len(pk) == 1:
            piece += " PRIMARY KEY"
        parts.append(piece)
    if len(pk) > 1:
        parts.append("    PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk) + ")")
    body = ",\n".join(parts)
    return f'CREATE TABLE IF NOT EXISTS "{table}" (\n{body}\n)'


def quote_ident(name: str) -> str:
    """Quote an SQL identifier."""

    return '"' + str(name).replace('"', '""') + '"'


def sql_literal(value) -> str:
    """Converts a Python value to a SQL literal that can be loaded into SQLite.

    Why it's needed: `--mode sql' mode output should be executable as well
    (`sqlite3 restored.db < restore.sql`) — nine placeholder lists with comments.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


# ─────────────────────────────────────────────────────────────
# PG connection layer (only where psycopg is required)
# ─────────────────────────────────────────────────────────────

def load_driver():
    """returns psycopg 3 (preferred) or psycopg2; None if none."""
    try:
        import psycopg  # type: ignore
        return ("psycopg3", psycopg)
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore
        return ("psycopg2", psycopg2)
    except ImportError:
        return None


def dsn_from_env() -> str:
    """Constructs a DSN from `PG_DSN' or libpq variables (a ready-to-connect string)."""
    dsn = os.getenv("PG_DSN", "").strip()
    if dsn:
        return dsn
    keys = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER")
    parts = [f"{k[2:].lower()}={os.getenv(k)}" for k in keys if os.getenv(k)]
    if os.getenv("PGPASSWORD"):
        parts.append("password=" + os.environ["PGPASSWORD"])
    return " ".join(parts)


def mask_dsn(dsn: str) -> str:
    """Removes password from DSN (for printing and monitoring; never log password)."""
    out = []
    for token in dsn.split():
        if token.lower().startswith("password="):
            out.append("password=***")
        else:
            out.append(token)
    return " ".join(out)


def pg_tables(cur) -> list:
    """Return the names of the tables in the PostgreSQL database."""

    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )
    return [r[0] for r in cur.fetchall()]


def pg_columns(cur, table: str) -> list:
    """[(name, sqlite_type, is_pk),...] in order of column position in PG."""
    cur.execute(
        """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    cols = cur.fetchall()
    cur.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = 'public' AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        """,
        (table,),
    )
    pk = {r[0] for r in cur.fetchall()}
    return [(c[0], sqlite_type(c[1], c[2]), c[0] in pk) for c in cols]


# ─────────────────────────────────────────────────────────────
# main route
# ─────────────────────────────────────────────────────────────

def run_export(args) -> int:
    """Export the PostgreSQL data to a SQLite file."""

    driver = load_driver()
    if driver is None:
        print("✗ psycopg/psycopg2 نصب نیست (پیش‌نیاز فاز E).", file=sys.stderr)
        print("  نصب: python -m pip install \"psycopg[binary]\"", file=sys.stderr)
        print("  (منطق ترجمه بدون PG قابل آزمون است: --self-test)", file=sys.stderr)
        return EXIT_NEEDS_ACTION
    drv_name, drv = driver

    dsn = args.dsn or dsn_from_env()
    if not dsn:
        print("✗ نشانی اتصال پیدا نشد: --dsn یا PG_DSN یا متغیرهای PGHOST/PGDATABASE/... لازم است.",
              file=sys.stderr)
        return EXIT_NEEDS_ACTION

    out = Path(args.out)
    if args.mode == "db":
        if out.exists() and not args.force:
            print(f"✗ مقصد موجود است: {out} — برای بازنویسی --force بدهید.", file=sys.stderr)
            return EXIT_NEEDS_ACTION
        live = _live_sqlite_path()
        if live and os.path.abspath(str(out)) == os.path.abspath(live) and not args.force:
            print(f"⛔ مقصد همان دیتابیس زنده‌ی SQLite است ({live}). این کار داده‌ی جاری را بازنویسی می‌کند؛"
                  " اگر واقعاً می‌خواهید، --force بدهید.", file=sys.stderr)
            return EXIT_NEEDS_ACTION
        if out.exists() and args.force:
            # Meaning of --force: "build from scratch"; Otherwise, insert a duplicate line in the previous file
            # or gives a primary key violation (not a rewrite).
            try:
                out.unlink()
                print(f"→ مقصد قبلی حذف شد (--force): {out}")
            except OSError as exc:
                print(f"✗ حذف مقصد ممکن نشد ({out}): {exc}", file=sys.stderr)
                return EXIT_ERROR

    print(f"→ درایور: {drv_name} | DSN: {mask_dsn(dsn)}")
    import sqlite3

    row_counts = {}
    naive_seen = {}
    sqlite_conn = sqlite3.connect(str(out)) if args.mode == "db" else None
    sql_out = None if args.mode == "db" else (open(args.out, "w", encoding="utf-8")
                                              if args.out and args.out != "-" else sys.stdout)

    try:
        with drv.connect(dsn) as conn:
            cur = conn.cursor()
            tables = [t for t in pg_tables(cur) if not args.tables or t in args.tables]
            if not tables:
                print("✗ هیچ جدولی با این فیلتر پیدا نشد.", file=sys.stderr)
                return EXIT_ERROR
            unknown = [t for t in tables if t not in KNOWN_TABLES]
            if unknown and not args.quiet:
                print(f"⚠ جدول‌های ناشناخته (خارج از فهرست فاز E): {', '.join(unknown)}")
            if sql_out is not None:
                sql_out.write("-- برون‌بری از PostgreSQL — %s\n" % _dt.datetime.now(_dt.timezone.utc)
                              .isoformat(timespec="seconds"))
                sql_out.write("BEGIN;\n")

            for table in tables:
                cols = pg_columns(cur, table)
                if not cols:
                    continue
                names = [c[0] for c in cols]
                select = "SELECT " + ", ".join(quote_ident(n) for n in names) + \
                         f" FROM {quote_ident(table)}"
                if args.order_by and args.order_by in names:
                    select += f" ORDER BY {quote_ident(args.order_by)}"
                cur.execute(select)

                if sqlite_conn is not None:
                    sqlite_conn.execute(sqlite_ddl(table, cols))

                placeholders = ", ".join(["?"] * len(names))
                insert = (f'INSERT INTO {quote_ident(table)} '
                          f'({", ".join(quote_ident(n) for n in names)}) VALUES ({placeholders})')

                if sql_out is not None:
                    # DDL as a comment: Schema creation is the job of the application itself; this
                    # Lines are for visual comparison/review only.
                    for line in sqlite_ddl(table, cols).splitlines():
                        sql_out.write("-- " + line + "\n")

                n = 0
                naive = 0
                batch = []
                while True:
                    rows = cur.fetchmany(1000)
                    if not rows:
                        break
                    for row in rows:
                        values = []
                        for value, (_, col_type, _pk) in zip(row, cols):
                            v, was_naive = to_sqlite_value(value, col_type)
                            naive += int(was_naive)
                            values.append(v)
                        n += 1
                        if sqlite_conn is not None:
                            batch.append(tuple(values))
                            if len(batch) >= 2000:
                                sqlite_conn.executemany(insert, batch)
                                batch = []
                        else:
                            sql_out.write(insert.replace("?", "{}").format(
                                *[sql_literal(v) for v in values]) + ";\n")
                    if sqlite_conn is not None and batch:
                        sqlite_conn.executemany(insert, batch)
                        batch = []
                row_counts[table] = n
                if naive:
                    naive_seen[table] = naive
                if not args.quiet:
                    flag = f"  ⚠ naive={naive}" if naive else ""
                    print(f"   {table:<26} {n:>8} سطر{flag}")

            if sqlite_conn is not None:
                sqlite_conn.commit()
            else:
                sql_out.write("COMMIT;\n")
    finally:
        if sqlite_conn is not None:
            sqlite_conn.close()
        if sql_out is not None and sql_out is not sys.stdout:
            sql_out.close()

    manifest = {
        "exported_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source_dsn": mask_dsn(dsn),
        "driver": drv_name,
        "mode": args.mode,
        "out": str(out),
        "row_counts": row_counts,
        "total_rows": sum(row_counts.values()),
        "tables": len(row_counts),
        "naive_timestamps": naive_seen,
    }
    if args.mode == "db":
        manifest["sha256"] = sha256_file(out)

    print("\n✔ برون‌بری کامل شد: %d جدول، %d سطر" % (len(row_counts), manifest["total_rows"]))
    if manifest.get("sha256"):
        print("  sha256:", manifest["sha256"])
    if naive_seen:
        print("  ⚠ timestampهای naive (نقض الزام آگاه-UTC فاز E):",
              ", ".join(f"{t}={n}" for t, n in naive_seen.items()))

    if args.manifest:
        mpath = Path(args.manifest if isinstance(args.manifest, str) else str(out) + ".manifest.json")
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  مانیتور:", mpath)
    return EXIT_OK


def _live_sqlite_path():
    """Reads the live SQLite database path from `config/settings.py`.

    Why don't we import? Import `config.settings` when running, warning
    It wakes up the SECRET_KEY and side paths and binds the tool to the application.
    Here we just read the constant value of `DB_PATH` with the AST — without any side effects.
    """
    import ast  # noqa: PLC0415
    settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.py"
    try:
        tree = ast.parse(settings_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — Missing/broken config should not block tools
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "DB_PATH" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────
# self-test (without PostgreSQL; tests the same translation logic)
# ─────────────────────────────────────────────────────────────

def self_test() -> int:
    """Run the built-in self-test of the export conversion rules."""

    import sqlite3

    fails = []

    def eq(label, got, want):
        """Record a failure when a value differs from the expected one."""
        if got != want:
            fails.append(f"{label}: got={got!r} want={want!r}")

    # 1) Type map
    eq("bool→INTEGER", sqlite_type("boolean", "bool"), "INTEGER")
    eq("int8→INTEGER", sqlite_type("bigint", "int8"), "INTEGER")
    eq("timestamptz→TEXT", sqlite_type("timestamp with time zone", "timestamptz"), "TEXT")
    eq("jsonb→TEXT", sqlite_type("jsonb", "jsonb"), "TEXT")
    eq("numeric→REAL", sqlite_type("numeric", "numeric"), "REAL")
    eq("text نامعلوم→TEXT", sqlite_type("character varying", "varchar"), "TEXT")

    # 2) Value conversion
    eq("bool True→1", to_sqlite_value(True, "INTEGER"), (1, False))
    eq("None→None", to_sqlite_value(None, "TEXT"), (None, False))
    eq("dict→JSON", to_sqlite_value({"a": 1}, "TEXT")[0], '{"a": 1}')
    eq("bytes می‌ماند", to_sqlite_value(b"\x00\x01", "BLOB")[0], b"\x00\x01")
    aware = _dt.datetime(2026, 10, 3, 15, 30, tzinfo=_dt.timezone(_dt.timedelta(hours=3, minutes=30)))
    eq("aware→UTC ISO", to_sqlite_value(aware, "TEXT")[0], "2026-10-03T12:00:00.000000+00:00")
    eq("aware naive=False", to_sqlite_value(aware, "TEXT")[1], False)
    naive_dt = _dt.datetime(2026, 10, 3, 12, 0, 0)
    eq("naive تشخیص داده می‌شود", to_sqlite_value(naive_dt, "TEXT")[1], True)
    eq("Decimal صحیح→int", to_sqlite_value(__import__("decimal").Decimal("42"), "INTEGER")[0], 42)

    # 3) DDL
    ddl = sqlite_ddl("t", [("ip", "TEXT", True), ("n", "INTEGER", False)])
    eq("DDL کلید اصلی", 'PRIMARY KEY' in ddl and '"ip" TEXT PRIMARY KEY' in ddl, True)
    ddl2 = sqlite_ddl("t2", [("a", "TEXT", True), ("b", "TEXT", True)])
    eq("DDL کلید مرکب", 'PRIMARY KEY ("a", "b")' in ddl2, True)

    # 4) Real insert cycle in SQLite (DDL phoenix + value translation)
    conn = sqlite3.connect(":memory:")
    cols = [("id", "INTEGER", True), ("ok", "INTEGER", False), ("payload", "TEXT", False)]
    conn.execute(sqlite_ddl("demo", cols))
    insert = 'INSERT INTO "demo" ("id", "ok", "payload") VALUES (?, ?, ?)'
    conn.executemany(insert, [(1, 1, '{"x": 1}'), (2, 0, None)])
    eq("شمارش سطر", conn.execute('SELECT COUNT(*) FROM "demo"').fetchone()[0], 2)
    eq("خواندن bool", conn.execute('SELECT ok FROM "demo" WHERE id=1').fetchone()[0], 1)
    eq("خواندن JSON", json.loads(conn.execute('SELECT payload FROM "demo" WHERE id=1').fetchone()[0]),
       {"x": 1})
    conn.close()

    # 5) SQL literal for --mode sql mode
    eq("لیترال NULL", sql_literal(None), "NULL")
    eq("لیترال bool", sql_literal(True), "1")
    eq("لیترال نقل‌قول فارسی", sql_literal("سلا\'م"), "'سلا''م'")
    eq("لیترال بایت", sql_literal(b"\x00\xff"), "X'00ff'")

    # 6) Masking the password in DSN
    eq("mask password", mask_dsn("host=h dbname=d password=secret user=u"),
       "host=h dbname=d password=*** user=u")

    # 7) Live database path without import
    eq("DB_PATH از AST", _live_sqlite_path(), "logs.db")

    # 8) sha256
    tmp = Path("/tmp/_export_selftest.bin")
    tmp.write_bytes(b"abc")
    eq("sha256", sha256_file(tmp), hashlib.sha256(b"abc").hexdigest())
    tmp.unlink()

    if fails:
        print("✗ خودآزمون شکست خورد:")
        for f in fails:
            print("   -", f)
        return EXIT_ERROR
    print("✔ خودآزمون: ۳۰ بررسی منطق ترجمه سبز (بدون نیاز به PostgreSQL)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""

    p = argparse.ArgumentParser(
        prog="export_from_pg.py",
        description="برون‌بری PostgreSQL → SQLite (مسیر بازگشتِ پس از cutover — فاز E).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="مثال:\n"
               "  python tools/export_from_pg.py --out archives/logs-after-cutover.db --manifest\n"
               "  python tools/export_from_pg.py --mode sql --tables logs,users > restore.sql\n"
               "  python tools/export_from_pg.py --self-test\n",
    )
    p.add_argument("--dsn", default="", help="نشانی اتصال PG (یا PG_DSN در محیط)")
    p.add_argument("--out", default="", help="فایل مقصد (.db) یا '-' برای stdout در حالت sql")
    p.add_argument("--mode", choices=("db", "sql"), default="db", help="db = فایل SQLite کامل؛ sql = INSERTها")
    p.add_argument("--tables", default="", help="فهرست جدول‌ها با کاما (پیش‌فرض: همه‌ی جدول‌های public)")
    p.add_argument("--order-by", default="", help="نام ستون برای مرتب‌سازی در SELECT (اختیاری)")
    p.add_argument("--force", action="store_true", help="بازنویسی مقصد موجود")
    p.add_argument("--manifest", nargs="?", const=True, default="",
                   help="نوشتن مانیتور JSON (پیش‌فرض: کنار فایل خروجی)")
    p.add_argument("--quiet", action="store_true", help="کم‌گویی")
    p.add_argument("--self-test", action="store_true", help="آزمون منطق ترجمه بدون PG")
    return p


def main(argv=None) -> int:
    """Command-line entry point of the PostgreSQL export."""

    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()
    if args.mode == "sql" and not args.out:
        args.out = "-"          # Default: print to stdout
    if not args.tables:
        allowed = None
    else:
        allowed = {t.strip() for t in args.tables.split(",") if t.strip()}
    args.tables = allowed
    return run_export(args)


if __name__ == "__main__":
    sys.exit(main())
