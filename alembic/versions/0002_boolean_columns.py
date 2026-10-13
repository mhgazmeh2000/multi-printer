# -*- coding: utf-8 -*-
"""
0002_boolean_columns — تبدیل پنج ستون پرچمی به ``BOOLEAN`` روی PostgreSQL (فاز E.4.1).

چرا این migration لازم است؟
---------------------------
ستون‌های پرچمی امروز `INTEGER 0/1` هستند (هم در schema امروزِ SQLite و هم در
migration `0001`). روی PostgreSQL این الگو کار می‌کند ولی معنای نوع را از دست
می‌دهد و در مقایسه‌ها اجبار به نوشتن `= 1` می‌کند. فاز E.4 تصمیم گرفت این پنج ستون
**نوع واقعی BOOLEAN** بگیرند (`docs/phase-e-plan.md` بند ۵ تصمیم‌های E.6):

    users.is_verified · users.email_verified · users.is_active
    yield_samples.accepted · security_events.success

روی SQLite این migration **no-op** است
--------------------------------------
SQLite نوع `BOOLEAN` ندارد؛ هر مقدار بولی به‌صورت `INTEGER 0/1` ذخیره می‌شود و این
«شکل کانونیک» خودِ SQLite است. پس schema امروزِ SQLite دست‌نخورده می‌ماند و رفتار
برنامه عوض نمی‌شود. کد پایتون هم برای هر دو backend یکسان است: مقادیر Python
``True``/``False`` فرستاده می‌شوند که sqlite3 آن‌ها را `1/0` می‌نویسد و psycopg
به `boolean` نگاشت می‌کند (اندازه‌گیری‌شده در E.4.1: نوشتن `int` روی ستون
`boolean` خطا می‌دهد ⇒ همه‌ی نویسندگان این پنج ستون به `bool` منتقل شدند).

ترتیب سه‌گانه‌ی ALTER (اندازه‌گیری‌شده، نه حدس)
---------------------------------------------
`ALTER COLUMN … TYPE BOOLEAN` وقتی ستون `DEFAULT 0/1` دارد شکست می‌خورد:

    ERROR: default for column "x" cannot be cast automatically to type boolean

پس هر ستون سه گام می‌رود:

    ۱) DROP DEFAULT
    ۲) TYPE BOOLEAN USING ("col"::boolean)      — cast عددی: ۰ ⇒ false، غیرصفر ⇒ true
    ۳) SET DEFAULT false/true                   — همان معنای پیش‌فرض قبلی

`NOT NULL`/`nullable` دست‌نخورده می‌ماند (این migration فقط نوع را عوض می‌کند):
سه ستون `users` سخت‌گیرانه (`NOT NULL`) و `yield_samples.accepted` /
`security_events.success` پذیرای NULL می‌مانند.

ایمنی اجرا
----------
• **idempotent:** اگر ستون از قبل `boolean` باشد، همان ستون رد می‌شود؛ پس اجرای
  دوباره (یا اجرا روی دیتابیسی که دستی تبدیل شده) خطا نمی‌دهد.
• **صریح، نه بی‌صدا:** اگر ستونی وجود نداشته باشد یا نوعش نه عددی باشد و نه
  `boolean`، اجرا با پیام روشن متوقف می‌شود (به‌جای تبدیل حدسی).
• **مسیر برگشت:** `downgrade` همان سه گام را برعکس می‌رود (`BOOLEAN` ⇒ `INTEGER`
  با `USING ("col"::int)` و برگرداندن پیش‌فرض `0/1`).

نکته‌ی مهم برای migrationهای بعدی
----------------------------------
`0001` عمداً دست‌نخورده ماند (قید فاز E: تغییر migration قبلی ممنوع). یعنی یک
نصب تازه روی PG از `0001` (INTEGER) شروع می‌کند و `0002` آن را به BOOLEAN می‌برد —
همان مسیر دیتابیس موجود.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

# شناسه‌ی یکتای این migration
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

#: (جدول، ستون، پیش‌فرضِ بولیِ جدید، پیش‌فرضِ عددیِ قبلی)
BOOLEAN_COLUMNS = (
    ("users", "is_verified", False, 0),
    ("users", "email_verified", False, 0),
    ("users", "is_active", True, 1),
    ("yield_samples", "accepted", True, 1),
    ("security_events", "success", False, 0),
)

#: نام‌های نوعی که «عددی» شمرده می‌شوند (پیش از تبدیل، همین انتظار می‌رود).
_INTEGER_TYPES = ("integer", "smallint", "bigint")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _pg_column_type(conn, table: str, column: str):
    """نوع فعلی ستون از ``information_schema`` (``None`` اگر ستون نباشد)."""
    row = conn.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return row[0] if row else None


def _sqlite_has_column(conn, table: str, column: str) -> bool:
    """آیا ستون در جدول SQLite هست؟ (فقط برای بررسی حضور، نه نوع.)"""
    rows = conn.execute(sa.text(f'PRAGMA table_info("{table}")')).fetchall()
    return any(r[1] == column for r in rows)


def _pg_convert(conn, table: str, column: str, to_boolean: bool, default) -> None:
    """
    تبدیل نوع یک ستون روی PG با همان سه‌گام اندازه‌گیری‌شده.

    ``DROP DEFAULT`` روی ستونی که پیش‌فرض ندارد هم بدون خطا انجام می‌شود، پس لازم
    نیست وجود پیش‌فرض را جدا بررسی کنیم.
    """
    target = "BOOLEAN" if to_boolean else "INTEGER"
    using = f'"{column}"::boolean' if to_boolean else f'"{column}"::int'
    if default is True:
        default_sql = "true"
    elif default is False:
        default_sql = "false"
    else:
        default_sql = str(default)

    op.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT')
    op.execute(
        f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE {target} '
        f"USING ({using})"
    )
    op.execute(
        f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {default_sql}'
    )


def upgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres():
        # ── SQLite: no-op ────────────────────────────────────────────────
        # SQLite نوع BOOLEAN ندارد؛ 0/1 ذخیره‌سازی کانونیک آن است و schema امروز
        # برنامه هم همین است. فقط **حضور** ستون‌ها بررسی می‌شود تا schema ناقص
        # بی‌صدا از این گام رد نشود (خطای روشن، نه تبدیل حدسی).
        missing = [f"{t}.{c}" for t, c, _new, _old in BOOLEAN_COLUMNS
                   if not _sqlite_has_column(conn, t, c)]
        if missing:
            raise RuntimeError(
                "0002_boolean_columns: ستون‌های زیر پیدا نشدند: " + ", ".join(missing) +
                " — schema SQLite با انتظار این migration هم‌خوان نیست "
                "(ابتدا init_db/alembic 0001 را اجرا کنید)."
            )
        log.info(
            "0002_boolean_columns: SQLite ⇒ no-op (نوع BOOLEAN ندارد؛ 0/1 شکل "
            "کانونیک آن است). %d ستون بررسی و تأیید شد.", len(BOOLEAN_COLUMNS)
        )
        return

    for table, column, new_default, _old_default in BOOLEAN_COLUMNS:
        current = _pg_column_type(conn, table, column)
        if current is None:
            raise RuntimeError(
                f"0002_boolean_columns: ستون «{table}.{column}» در دیتابیس نیست؛ "
                "schema با انتظار این migration هم‌خوان نیست."
            )
        if current == "boolean":
            log.info("0002_boolean_columns: %s.%s از قبل boolean است ⇒ رد شد.",
                     table, column)
            continue
        if current not in _INTEGER_TYPES:
            raise RuntimeError(
                f"0002_boolean_columns: نوع «{table}.{column}» = {current!r} است، "
                f"نه یکی از {_INTEGER_TYPES} — تبدیل خودکار بی‌صدا مجاز نیست."
            )
        # NOT NULL / nullable دست‌نخورده می‌ماند (این گام فقط نوع و پیش‌فرض را عوض می‌کند).
        _pg_convert(conn, table, column, to_boolean=True, default=new_default)
        log.info("0002_boolean_columns: %s.%s ⇒ BOOLEAN (default=%s)",
                 table, column, new_default)


def downgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres():
        # مثل upgrade: روی SQLite چیزی برای برگرداندن نیست.
        log.info("0002_boolean_columns (downgrade): SQLite ⇒ no-op.")
        return

    for table, column, _new_default, old_default in BOOLEAN_COLUMNS:
        current = _pg_column_type(conn, table, column)
        if current is None:
            raise RuntimeError(
                f"0002_boolean_columns (downgrade): ستون «{table}.{column}» نیست."
            )
        if current in _INTEGER_TYPES:
            log.info("0002_boolean_columns (downgrade): %s.%s از قبل عددی است ⇒ رد شد.",
                     table, column)
            continue
        if current != "boolean":
            raise RuntimeError(
                f"0002_boolean_columns (downgrade): نوع «{table}.{column}» = {current!r} "
                "است، نه boolean — برگرداندن خودکار مجاز نیست."
            )
        _pg_convert(conn, table, column, to_boolean=False, default=old_default)
        log.info("0002_boolean_columns (downgrade): %s.%s ⇒ INTEGER (default=%s)",
                 table, column, old_default)
