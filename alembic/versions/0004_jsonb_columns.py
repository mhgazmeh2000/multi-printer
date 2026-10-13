# -*- coding: utf-8 -*-
"""
0004_jsonb_columns — ``logs.details`` روی PostgreSQL ⇒ ``jsonb`` (فاز E.6.4).

چرا؟
----
معیار مالک برای این تصمیم «``grep json_extract``» بود: ستونی به JSONB می‌رود که
**SQL** روی آن محتوای JSON می‌خواند. نتیجهٔ گرِپ (و تنها نتیجهٔ آن):

    web/routes/metrics.py        details->>'detection'      (GROUP BY روی همهٔ رویدادهای کارتریج)
    core/database.py             details->>'event_id' / 'prev_total' / 'current_total'  (dedup)

یعنی فقط ``logs.details``. چهار ستون JSON دیگر (``users.allowed_offices``،
``users.allowed_modules``، ``printer_counters.cartridge_id_state``،
``printer_counters.toner_overrides``) هیچ‌جا در SQL به‌صورت JSON خوانده نمی‌شوند
(فقط ``json.dumps``/``json.loads`` در پایتون)؛ تبدیل آن‌ها ریسک migration و
واگرایی با SQLite را بی‌سود اضافه می‌کرد، پس **TEXT می‌مانند** (تصمیم و عددِ
اندازه‌گیری در ``docs/phase-e-decisions.md``، ردیف E.6.4).

امروز روی PG هلپر ``core.db.ConnectionAdapter.json_extract`` چنین می‌سازد:
``(details::jsonb)->>'key'`` — یعنی **هر ردیفِ اسکن‌شده یک بار parse می‌شود**.
اندازه‌گیری روی همین سرور (PG 16.15، ۲۰۰٬۰۰۰ ردیف، ۱۳۳٬۰۰۰ ردیف
``CARTRIDGE_CHANGED``، کوئری metrics):

    TEXT + cast   ≈ ۱۱۹٫۰ / ۱۱۹٫۱ / ۱۱۹٫۷ ms      (میانگین ۱۱۹٫۳)
    jsonb         ≈  ۴۶٫۵ /  ۴۶٫۳ /  ۴۷٫۵ ms      (میانگین ۴۶٫۸  ⇒ ۲٫۵× سریع‌تر)
    dedup (LIMIT 1): ۱۹٫۳ ms ⇒ ۱۷٫۸ ns (ناچیز — همان‌طور که انتظار می‌رفت، چون
                      روی چند ردیفِ یک پرینتر در ۵ دقیقه کار می‌کند)
    expression index روی TEXT کمکی نکرد: ۱۱۶ ms (پلن همان seq scan می‌ماند)
    نوشتن ۲۰٬۰۰۰ ردیف: text ۷۹ ms ⇒ jsonb ۳۶ ms   حجم جدول: هر دو ۴۰ MB

کوئری metrics هر scrape پرامتئوس اجرا می‌شود، پس این هزینهٔ تکرارشونده است.
دومین فایده، **اعتبار در زمان نوشتن** است: امروز یک ردیف با ``details`` نامعتبر
(دستی/میراثی) کل کوئری را با خطای cast می‌شکند؛ با jsonb همان ردیف در *نوشتن*
رد می‌شود، نه در *خواندن* — خطا کنار علتش می‌افتد.

روی SQLite این migration **no-op** است
-------------------------------------
SQLite نوع JSON ندارد؛ همان ``TEXT`` می‌ماند و ``json_extract`` کار می‌کند. این
migration فقط بررسی می‌کند ستون در SQLite واقعاً متنی باشد (و اگر نبود، با پیام
روشن می‌ایستد) تا دو backend از «انتظارِ اسکیما» جدا نشوند. خواننده‌ها هر دو
نوع را تحمل می‌کنند: psycopg مقدار ``jsonb`` را دیکد‌شده (dict) برمی‌گرداند و
``core/database.py::_row_to_dict`` هر دو را می‌پذیرد.

ایمنی
----
• **preflight اجباری:** پیش از ALTER، ردیف‌های نامعتبر با یک تابع ``pg_temp`` که
  ``EXCEPTION WHEN others`` دارد شمرده می‌شوند (در PG ۱۶ هیچ ``IS JSON`` و هیچ
  try-cast وجود ندارد؛ این تنها راهِ بی‌استثناست). اگر حتی یک ردیف نامعتبر باشد ⇒
  **توقف** با شمارش و فهرست idها (تصمیم مالک در E.6.4: «توقف با شمارش»، نه NULL
  کردن بی‌صدا).
• ``''`` و فاصلهٔ خالی: ``NULL`` می‌شوند و این **تغییر داده** است، پس پیش از اجرا
  **هشدار با شمارش** می‌دهد (بی‌صدا رد نمی‌شود).
• **idempotent:** اگر ستون از قبل ``jsonb`` بود، فقط بررسی و رد.
• **پایش پس از اجرا:** نوع ستون دوباره از ``information_schema`` خوانده می‌شود؛
  اگر jsonb نبود، migration می‌ایستد (هیچ «انجام شد» بی‌پشتوانه‌ای نداریم).
• **برگشت:** ``downgrade`` به ``text`` برمی‌گردد (``USING (details)::text``)؛ ردیف‌هایی
  که ``''`` بوده‌اند در آنجا ``NULL`` می‌مانند — این تنها تفاوتِ برگشتناپذیر است و
  در همین‌جا مستند شده، نه پنهان.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

#: تنها ستون‌هایی که این migration تبدیل می‌کند (تصمیم E.6.4 — با معیار grep).
#: اگر روزی ستون تازه‌ای لازم شد، همین فهرست + تستِ قفلش باید عوض شود.
JSONB_COLUMNS = (
    ("logs", "details"),
)

#: نوع متنیِ معادل در SQLite (که اینجا تغییر نمی‌کند؛ فقط بررسی می‌شود).
SQLITE_TEXT_TYPES = ("TEXT", "")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _exec(conn, sql: str):
    """
    اجرای SQL روی اتصال alembic (SQLAlchemy) **یا** اتصال خام psycopg.

    چرا: تابع‌های بررسی/preflight این migration با `op.get_bind()` اجرا می‌شوند،
    ولی دقیقاً همان منطق (شمارش ردیف‌های JSON نامعتبر) باید در تست‌ها با یک اتصال
    خام و یک جدول موقت آزموده شود — بدون اینکه migration برای تست «دوباره نوشته»
    یا کپی شود. SQLAlchemy `text()` روی اتصال خام psycopg خطا می‌دهد
    («expected bytes, TextClause found»؛ سنجیده‌شده)، پس نوع اتصال تشخیص داده
    می‌شود.
    """
    if hasattr(conn, "dialect"):
        return conn.execute(sa.text(sql))
    return conn.execute(sql)


def _column_type(conn, table: str, column: str):
    """نوع ستون از ``information_schema`` در ** اسکیمای جاری ** (search_path)."""
    row = _exec(conn, "SELECT data_type FROM information_schema.columns "
                       "WHERE table_schema = current_schema() "
                       f"AND table_name = '{table}' AND column_name = '{column}'").fetchone()
    return None if row is None else row[0]


def _sqlite_column_decl(conn, table: str, column: str):
    """تعریف ستون از ``sqlite_master`` (برای اطمینان از «متنی بودن» در SQLite)."""
    row = _exec(conn, f"SELECT sql FROM sqlite_master WHERE type='table' "
                      f"AND name = '{table}'").fetchone()
    if row is None or not row[0]:
        return None
    for line in str(row[0]).splitlines():
        stripped = line.strip().strip(",")
        if stripped.lower().startswith(f'"{column}"') or stripped.lower().startswith(
                f"{column} ") or stripped.lower().startswith(f"{column}\" "):
            return stripped
    return None


def _invalid_where(table: str, column: str) -> str:
    """شرط «ردیف نامعتبر» — برای شمارش و برای فهرست idها (یکی، تا واگرا نشوند)."""
    return (f'"{column}" IS NOT NULL AND btrim("{column}") <> \'\' '
            f'AND NOT pg_temp.pg_json_is_valid("{column}")')


def _empty_where(table: str, column: str) -> str:
    """شرط «ردیف تهی»: رشتهٔ خالی/فاصله، که با migration به NULL تبدیل می‌شود."""
    return f'"{column}" IS NOT NULL AND btrim("{column}") = \'\''


_PG_TEMP_VALIDATOR = """
CREATE OR REPLACE FUNCTION pg_temp.pg_json_is_valid(t text) RETURNS boolean AS $$
BEGIN
    PERFORM t::jsonb;
    RETURN true;
EXCEPTION WHEN others THEN
    RETURN false;
END;
$$ LANGUAGE plpgsql
"""


def _preflight(conn, table: str, column: str) -> None:
    """
    شمارش ردیف‌های نامعتبر/تهی؛ نامعتبر ⇒ توقف (تصمیم مالک در E.6.4: بی‌صدا NULL نه).

    چرا تابع pg_temp با exception handler: در PG ۱۶ نه ``IS JSON`` هست نه try-cast،
    و ``details::jsonb`` داخل ``WHERE`` خودش خطا می‌دهد ⇒ تنها راهِ «پیدا کردن
    ردیف‌های خراب بدون شکستن کوئری». تابع در ``pg_temp`` است و در پایان همین
    جلسه/اسکیمای موقت از بین می‌رود (شیء دائمی ساخته نمی‌شود).

    چرا دو کوئری به‌جای ``array_agg(id)[:20]``: SQLAlchemy داخل ``text()`` علامت
    ``:20`` را bind-parameter می‌خواند (سنجیده‌شده: «A value is required for bind
    parameter '20'») — پس فهرست idها جدا با ``LIMIT`` گرفته می‌شود.
    """
    _exec(conn, _PG_TEMP_VALIDATOR)
    try:
        bad_where = _invalid_where(table, column)
        bad_count = int(_exec(conn, f'SELECT count(*) FROM "{table}" WHERE {bad_where}')
                        .fetchone()[0])
        if bad_count:
            ids = [str(r[0]) for r in _exec(
                conn, f'SELECT id FROM "{table}" WHERE {bad_where} ORDER BY id LIMIT 20'
            ).fetchall()]
            more = "…" if bad_count > len(ids) else ""
            raise RuntimeError(
                f"0004_jsonb_columns: {bad_count} ردیف در «{table}.{column}» JSON معتبر "
                f"نیستند ⇒ migration متوقف شد (تصمیم E.6.4: داده بی‌صدا دور ریخته نمی‌شود). "
                f"نمونه idها: {', '.join(ids)}{more}. برای یافتنشان: "
                f'SELECT id, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL '
                f"AND btrim(\"{column}\") <> '' AND btrim(\"{column}\") !~ '^[{{[]' ; "
                f"پس از اصلاح داده، دوباره `alembic upgrade head` را اجرا کنید."
            )
        empties = int(_exec(conn,
            f'SELECT count(*) FROM "{table}" WHERE {_empty_where(table, column)}'
        ).fetchone()[0])
        if empties:
            log.warning(
                "0004_jsonb_columns: %d ردیف در «%s.%s» رشتهٔ خالی دارند و با این "
                "migration NULL می‌شوند (تغییر داده؛ عمداً پیش از اجرا هشدار داده "
                "می‌شود).", empties, table, column)
    finally:
        _exec(conn, "DROP FUNCTION IF EXISTS pg_temp.pg_json_is_valid(t text)")


def upgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres():
        for table, column in JSONB_COLUMNS:
            decl = _sqlite_column_decl(conn, table, column)
            if decl is None:
                raise RuntimeError(
                    f"0004_jsonb_columns: جدول «{table}» یا ستون «{column}» در SQLite "
                    "پیدا نشد ⇒ schema با انتظار این migration هم‌خوان نیست."
                )
            upper = decl.upper()
            if not any(t in upper for t in ("TEXT", "VARCHAR", "CHAR")):
                raise RuntimeError(
                    f"0004_jsonb_columns: ستون «{table}.{column}» در SQLite متنی نیست "
                    f"({decl!r}) — انتظار TEXT بود؛ بررسی کنید چه چیزی این‌جا را عوض کرده."
                )
        log.info(
            "0004_jsonb_columns: SQLite ⇒ no-op (JSONB ندارد؛ %d ستون به‌عنوان TEXT "
            "بررسی و تأیید شد).", len(JSONB_COLUMNS))
        return

    for table, column in JSONB_COLUMNS:
        current = _column_type(conn, table, column)
        if current is None:
            raise RuntimeError(
                f"0004_jsonb_columns: ستون «{table}.{column}» در اسکیمای جاری "
                f"({_exec(conn, 'SELECT current_schema()').fetchone()[0]}) نیست."
            )
        if current == "jsonb":
            log.info("0004_jsonb_columns: %s.%s از قبل jsonb است ⇒ رد شد.", table, column)
            continue
        if current not in ("text", "character varying"):
            raise RuntimeError(
                f"0004_jsonb_columns: انتظار ستون متنی بود، نوع «{table}.{column}» = "
                f"{current!r} ⇒ خودسرانه تبدیل نمی‌شود."
            )

        _preflight(conn, table, column)

        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE jsonb '
            f"USING (CASE WHEN \"{column}\" IS NULL OR btrim(\"{column}\") = '' "
            f"THEN NULL ELSE \"{column}\"::jsonb END)"
        )

        after = _column_type(conn, table, column)
        if after != "jsonb":
            raise RuntimeError(
                f"0004_jsonb_columns: پس از ALTER نوع «{table}.{column}» = {after!r} شد، "
                "نه 'jsonb'."
            )
        log.info("0004_jsonb_columns: %s.%s ⇒ jsonb (اعتبار JSON از این پس در زمان "
                 "نوشتن بررسی می‌شود، نه در زمان خواندن).", table, column)


def downgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres():
        log.info("0004_jsonb_columns (downgrade): SQLite ⇒ no-op.")
        return

    for table, column in JSONB_COLUMNS:
        current = _column_type(conn, table, column)
        if current is None:
            raise RuntimeError(
                f"0004_jsonb_columns (downgrade): ستون «{table}.{column}» در دیتابیس نیست."
            )
        if current != "jsonb":
            log.info("0004_jsonb_columns (downgrade): %s.%s jsonb نیست (%r) ⇒ رد شد.",
                     table, column, current)
            continue
        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE text '
            f'USING (CASE WHEN "{column}" IS NULL THEN NULL ELSE "{column}"::text END)'
        )
        log.info("0004_jsonb_columns (downgrade): %s.%s ⇒ text. توجه: ردیف‌هایی که در "
                 "upgrade از '' به NULL تبدیل شده بودند در این حالت NULL می‌مانند.",
                 table, column)
