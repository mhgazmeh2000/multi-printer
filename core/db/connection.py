# -*- coding: utf-8 -*-
"""
ConnectionAdapter — لایه‌ی نازک روی اتصال دیتابیس (فاز D: SQLite، فاز E.2: دو backend).

قرارداد این لایه:
  • هیچ رفتاری از بیرون تغییر نمی‌کند. adapter یک **دورگیر** است، نه موتور جدید.
  • «استفاده از adapter» و «استفاده از منطق قبلی» در تست‌ها با
    ``assert_wire_equivalent`` کنار هم بسته می‌شوند؛ عمداً هیچ تفاوت معنایی در
    گروه‌بندی تراکنش یا ردیف‌های بازگشتی مجاز نیست (``executemany`` هم مثل قبل
    یک تراکنش واحد است، نه یک تراکنش به‌ازای‌هر دسته).
  • کوئری‌ها همچنان با سبک ``?`` نوشته می‌شوند؛ ترجمه‌ی placeholder فقط در
    مسیر PostgreSQL فعال است (``core/db/placeholders.py``).

وضعیت پس از E.2.1 (dual backend)
---------------------------------
  • ``SUPPORTED_BACKENDS`` هر دو مقدار ``sqlite`` و ``postgresql`` را دارد؛
  • ``sqlite`` ⇒ :func:`connect_sqlite` روی ``config.settings.DB_PATH``؛
  • ``postgresql`` ⇒ :func:`connect_postgres` روی ``PG_DSN`` با درایور ``psycopg`` ۳.
    اتصال با ``autocommit=False`` (پیش‌فرض psycopg) و **TimeZone=UTC قفل‌شده** در
    سطح session باز می‌شود (بند ۷ ``docs/postgresql-strictness.md``)؛
  • ``backend`` به هر دو قیمت نگاشت می‌شود: پیش‌فرض ``sqlite``، بدون fallback بی‌صدا.

نکته‌ی سازگاری: ``DB_BACKEND=postgresql`` در فاز E.2 **در production فعال نمی‌شود**؛
این لایه فقط پذیرنده‌ی backend دوم می‌شود (فعال‌سازی، فاز E.3).

افزوده‌ی E.4.3: ``ConnectionAdapter.insert_returning_id`` — «INSERT + شناسه‌ی
ردیف» با یک امضا روی هر دو backend (SQLite: ``lastrowid``؛ PG: ``RETURNING id``).
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from typing import Any, Iterable, Iterator, Optional, Sequence

from config import settings as _settings

from .exceptions import (
    ColumnAlreadyExists,
    ConfigurationError,
    DatabaseConnectionError,
    DatabaseError,
    DatabaseOperationalError,
    PlaceholderTranslationError,
    UndefinedColumn,
    UndefinedTable,
    UniqueViolation,
    UnsupportedBackendError,
)
from .placeholders import translate_placeholders
from .scripts import split_sql_statements

#: مقادیر مجاز پارامتر محیطی ``DB_BACKEND``. از E.2 هر دو backend پشتیبانی می‌شوند.
BACKEND_SQLITE = "sqlite"
BACKEND_POSTGRESQL = "postgresql"
SUPPORTED_BACKENDS = (BACKEND_SQLITE, BACKEND_POSTGRESQL)
ALL_BACKENDS = (BACKEND_SQLITE, BACKEND_POSTGRESQL)

ENV_DB_BACKEND = "DB_BACKEND"
#: نشانی اتصال PostgreSQL (سبک libpq: ``host=… port=… dbname=… user=… password=…``).
ENV_PG_DSN = "PG_DSN"

#: نام‌های رایج دیگر برای PostgreSQL (در فاز E به همان backend نگاشت می‌شوند).
BACKEND_ALIASES = {
    "postgres": BACKEND_POSTGRESQL,
    "pg": BACKEND_POSTGRESQL,
}


def resolve_backend(value: Optional[str] = None) -> str:
    """
    backend را از پارامتر یا متغیر محیطی ``DB_BACKEND`` تعیین می‌کند.

    از E.2 هر دو مقدار مجازند: ``sqlite`` و ``postgresql`` (با نام‌های جایگزین
    ``postgres``/``pg``). مقدار خالی ⇒ پیش‌فرض ``sqlite``.

    نکته: تعیین backend **به‌تنهایی** یعنی «backend انتخاب شد»، نه «اتصال ممکن است»؛
    برای postgresql، نبودِ ``PG_DSN`` در :func:`connect` با ``ConfigurationError``
    اعلام می‌شود (نه fallback بی‌صدا به sqlite).

    Raises:
        UnsupportedBackendError: مقدار ناشناخته برای ``DB_BACKEND``.
    """
    raw = value if value is not None else os.getenv(ENV_DB_BACKEND, "")
    backend = (raw or BACKEND_SQLITE).strip().lower()
    backend = BACKEND_ALIASES.get(backend, backend)
    if backend in SUPPORTED_BACKENDS:
        return backend
    raise UnsupportedBackendError(
        f"backend ناشناخته «{backend}» برای {ENV_DB_BACKEND} "
        f"(مقادیر مجاز: {', '.join(ALL_BACKENDS)}).",
        backend=backend)


def resolve_dsn(backend: str, dsn: Optional[str] = None,
                *, env: Optional[dict] = None) -> Optional[str]:
    """
    نشانی اتصالِ نهایی را برای backend تعیین می‌کند — **صریح، بدون fallback**.

    Args:
        backend: مقدار بازگشتی :func:`resolve_backend`.
        dsn: نشانی صریح (اولویت اول)؛ ``None`` ⇒ از ``PG_DSN``.
        env: mapping جایگزین محیط (برای تست).

    Returns:
        برای ``postgresql``: رشته‌ی DSN غیرخالی. برای ``sqlite``: ``None``
        (sqlite نشانی ندارد؛ مسیر فایل جداگانه با ``path`` می‌آید).

    Raises:
        UnsupportedBackendError: backend ناشناخته.
        ConfigurationError: ``postgresql`` بدون هیچ نشانی.
    """
    if backend == BACKEND_SQLITE:
        return None
    if backend != BACKEND_POSTGRESQL:
        raise UnsupportedBackendError(
            f"backend «{backend}» مسیر اتصالی ندارد "
            f"(مقادیر مجاز: {', '.join(ALL_BACKENDS)}).", backend=backend)
    target = (dsn if dsn is not None else dsn_from_env(env)).strip()
    if not target:
        raise ConfigurationError(
            f"backend=postgresql انتخاب شده ولی نشانی اتصال خالی است؛ "
            f"{ENV_PG_DSN} را ست کنید (مثال: host=127.0.0.1 port=5432 dbname=printguard "
            f"user=printguard password=…) یا backend را به {BACKEND_SQLITE} برگردانید.",
            backend=BACKEND_POSTGRESQL)
    return target


def dsn_from_env(env: Optional[dict] = None) -> str:
    """
    نشانی اتصال PostgreSQL را از ``PG_DSN`` می‌خواند (خالی ⇒ رشته‌ی خالی).

    Args:
        env: mapping جایگزین (پیش‌فرض ``os.environ``) — برای تست.
    """
    source = os.environ if env is None else env
    return (source.get(ENV_PG_DSN, "") or "").strip()


def default_db_path() -> str:
    """
    مسیر دیتابیسِ پیش‌فرض را **در زمان فراخوانی** از ``config.settings`` می‌خواند.

    چرا مهم است: اگر مسیر در زمان import بایند شود، monkeypatch کردن
    ``config.settings.DB_PATH`` (الگوی تست‌ها و ابزارها) بی‌اثر می‌شود. اینجا هر
    بار مقدار فعلی خوانده می‌شود. مقدار پیش‌فرض ``logs.db`` نسبی است و در لحظه‌ی
    اتصال نسبت به CWD جاری تفسیر می‌شود — همان رفتاری که کد قبلی داشت.
    """
    return getattr(_settings, "DB_PATH", "logs.db")


# ─────────────────────────────────────────────────────────────────────
# ترجمه‌ی خطای درایور به خانواده‌ی مشترک (E.4.4)
# ─────────────────────────────────────────────────────────────────────
#: نگاشت پیام‌های SQLite به کلاس مشترک. SQLite کد SQLSTATE ندارد، پس ناچار
#: پیام‌محور است؛ این تنها جایی است که به متن پیام تکیه می‌کنیم و همان‌جا مستند
#: شده. ترتیب مهم است: بندهای خاص‌تر اول می‌آیند.
_SQLITE_MESSAGE_RULES = (
    ("duplicate column name", ColumnAlreadyExists),
    ("no such table", UndefinedTable),
    ("no such column", UndefinedColumn),
)

#: پیشوندهای SQLSTATE که به کلاس مشترک نگاشت می‌شوند (سمت PG).
_PG_SQLSTATE_RULES = (
    ("23", UniqueViolation),        # کل خانوادهٔ integrity (تصمیم E.4.4)
    ("42P01", UndefinedTable),
    ("42703", UndefinedColumn),
    ("42701", ColumnAlreadyExists),
)


#: کلید JSON باید «شناسهٔ ساده» باشد (بدون نقطه/کوتیشن/فاصله). این هِلپر متن SQL
#: می‌سازد، پس اعتبارسنجی ورودی این‌جا مرز امنیتی است.
_JSON_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    """
    شناسه (نام ستون/جدول) را برای درج در متن SQL نقل‌قول می‌کند.

    نحو استاندارد SQL: کوتیشن دوتایی، و هر کوتیشن داخلی دو برابر می‌شود. روی هر
    دو backend معتبر است (``core.db.schema`` هم از همین استفاده می‌کند).
    """
    return '"' + str(name).replace('"', '""') + '"'


def _translate_error(exc: BaseException, backend: str) -> BaseException:
    """
    خطای نیتیو درایور را به خانواده‌ی ``core.db.exceptions`` ترجمه می‌کند.

    قرارداد:
      • خطاهایی که از قبل ``DatabaseError`` هستند دست‌نخورده برمی‌گردند
        (ترجمهٔ دوباره ممنوع)؛
      • در SQLite نگاشت پیام‌محور است (کد SQLSTATE وجود ندارد)؛
      • در PG ابتدا ``sqlstate`` و بعد **خانوادهٔ استثنای psycopg** بررسی می‌شود
        (چون بعضی خطاها — مثل خطای کلاینت — sqlstate خالی دارند)؛
      • خطاهای ترجمه‌نشده **دست‌نخورده** برمی‌گردند تا رفتار قبلی حفظ شود
        (هیچ خطایی بی‌دلیل بازنویسی نمی‌شود)؛
      • ``original`` همیشه نگه داشته می‌شود و پیام درایور دست‌نخورده می‌ماند
        (چند مصرف‌کننده همچنان متن را برای راهنمایی کاربر می‌خوانند).

    Args:
        exc: استثنای گرفته‌شده از درایور.
        backend: ``sqlite`` یا ``postgresql``.

    Returns:
        استثنای مشترک (یا همان ``exc`` اگر خارج از نقشه باشد).
    """
    if isinstance(exc, DatabaseError):
        return exc

    message = str(exc)

    if backend == BACKEND_SQLITE:
        try:
            import sqlite3
            is_integrity = isinstance(exc, sqlite3.IntegrityError)
            is_operational = isinstance(exc, sqlite3.OperationalError)
            is_db_error = isinstance(exc, sqlite3.DatabaseError)
        except ImportError:  # pragma: no cover - دفاعی
            return exc
        if not is_db_error:
            return exc
        if is_integrity:
            return UniqueViolation(message, backend=backend, original=exc)
        lowered = message.lower()
        if is_operational:
            for needle, cls in _SQLITE_MESSAGE_RULES:
                if needle in lowered:
                    return cls(message, backend=backend, original=exc)
            return DatabaseOperationalError(message, backend=backend, original=exc)
        return DatabaseError(message, backend=backend, original=exc)

    # ── PostgreSQL ──
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate:
        for prefix, cls in _PG_SQLSTATE_RULES:
            if sqlstate.startswith(prefix):
                return cls(message, backend=backend, sqlstate=sqlstate, original=exc)
    try:
        import psycopg
        mapping = (
            (psycopg.errors.DuplicateColumn, ColumnAlreadyExists),
            (psycopg.errors.UndefinedTable, UndefinedTable),
            (psycopg.errors.UndefinedColumn, UndefinedColumn),
            (psycopg.IntegrityError, UniqueViolation),
            (psycopg.OperationalError, DatabaseOperationalError),
            (psycopg.Error, DatabaseError),
        )
    except ImportError:  # pragma: no cover - دفاعی
        return exc
    for cls, target in mapping:
        if isinstance(exc, cls):
            return target(message, backend=backend, sqlstate=sqlstate, original=exc)
    return exc


class ConnectionAdapter:
    """
    دور SQLite یک لایه‌ی نازک می‌کشد تا کد بالادستی به backend وابسته نباشد.

    استفاده (هم‌شکل با اتصال خام قبلی)::

        with db_connection(commit=True) as conn:
            conn.execute("INSERT INTO logs (...) VALUES (?, ?)", (a, b))
            row = conn.execute("SELECT ...", (x,)).fetchone()

    سازگاری‌ها:
      • ``fetchone``/``fetchall``/``fetchmany``/``description``/``rowcount``/
        ``lastrowid``/``close``/``iteration`` روی خود adapter فوروارد می‌شوند ⇒
        کدی که ``conn.execute(...).fetchone()`` می‌نوشت بدون تغییر کار می‌کند.
      • ``with conn.execute(...) as cur:`` (الگوی رایج sqlite3) کار می‌کند، چون
        ``__enter__`` روی adapter همان cursor را برمی‌گرداند.
      • ``iter(conn.execute(...))`` و ``for row in conn.execute(...)`` هم کار
        می‌کند (``__iter__`` فقط وقتی cursor فعال باشد معنا دارد).

    تفاوت‌های backend (فاز E.2، همه با اندازه‌گیری):
      • ``lastrowid`` ⇒ روی PG ``NotImplementedError`` با راه‌حل ``RETURNING id``؛
        راه درست از E.4.3 :meth:`insert_returning_id` است (روی هر دو backend کار می‌کند)؛
      • ``executescript`` ⇒ از E.4.7 روی PG هم کار می‌کند: اسکریپت به statement
        می‌شکند و هر کدام جداگانه اجرا می‌شود (پیمایشگر: ``core/db/scripts.py``)؛
      • خطاها ⇒ ترجمه‌شده به ``core.db.exceptions`` (E.4.4): ``UniqueViolation``،
        ``ColumnAlreadyExists``، ``UndefinedTable``/``UndefinedColumn``،
        ``DatabaseOperationalError``، و در نهایت ``DatabaseError``؛
      • ``row_factory`` ⇒ روی PG بی‌اثر (شکل ردیف با ``psycopg.rows`` در E.5)؛
      • ``executemany`` ⇒ روی PG از cursor می‌رود (``psycopg.Connection`` این متد را ندارد).
    """

    def __init__(self, conn, backend: str = BACKEND_SQLITE):
        # صریح: دورگیریِ اتصالی با backend ناشناخته، خطا را به زمان اجرای کوئری
        # می‌انداخت (پیام گنگ «ترجمه‌ی placeholder ناموفق بود»). این‌جا زودتر و روشن‌تر.
        if backend not in SUPPORTED_BACKENDS:
            raise UnsupportedBackendError(
                f"backend «{backend}» برای ConnectionAdapter پشتیبانی نمی‌شود "
                f"(مقادیر مجاز: {', '.join(SUPPORTED_BACKENDS)}).", backend=backend)
        self._conn = conn
        self.backend = backend
        self._closed = False
        self._last_cursor = None

    # ─── اجرای کوئری ─────────────────────────────────────────────
    def _sql(self, sql: str) -> str:
        """کوئری را برای backend فعلی آماده می‌کند (در SQLite: بدون تغییر)."""
        if self.backend == BACKEND_SQLITE:
            return sql
        try:
            return translate_placeholders(sql, self.backend)
        except PlaceholderTranslationError:
            raise
        except Exception as exc:  # pragma: no cover - نگهبان دفاعی
            raise PlaceholderTranslationError(
                f"ترجمه‌ی placeholder ناموفق بود: {exc}", backend=self.backend) from exc

    def execute(self, sql: str, params: Sequence[Any] = ()):
        """
        اجرای یک کوئری؛ cursor خام (با fetch* و پشتیبانی context manager) را برمی‌گرداند.

        خطای درایور پیش از بالا رفتن، به خانواده‌ی ``core.db.exceptions`` ترجمه
        می‌شود (E.4.4) تا مصرف‌کننده هیچ‌گاه به ``sqlite3.*``/``psycopg.*`` وابسته
        نباشد. استثنای ترجمهٔ placeholder (خطای خودِ adapter) دست‌نخورده می‌ماند.
        """
        try:
            cur = self._conn.execute(self._sql(sql), params)
        except DatabaseError:
            raise
        except Exception as exc:
            raise _translate_error(exc, self.backend) from exc
        self._last_cursor = cur
        return cur

    def executemany(self, sql: str, params_list: Iterable[Sequence[Any]]):
        """
        اجرای دسته‌ای یک کوئری.

        مثل قبل **یک تراکنش واحد** است: commit/rollback وظیفه‌ی فراخوان است
        (یا ``commit=True`` در ``db_connection``).

        تفاوت backend (اندازه‌گیری‌شده): ``sqlite3.Connection`` متد ``executemany``
        دارد ولی ``psycopg.Connection`` ندارد ⇒ روی postgresql همان کار از طریق
        cursor انجام می‌شود. سیاست تراکنش یکسان می‌ماند (هیچ commit ضمنی).
        """
        try:
            if self.backend == BACKEND_SQLITE:
                cur = self._conn.executemany(self._sql(sql), params_list)
            else:
                cur = self._conn.cursor()
                cur.executemany(self._sql(sql), params_list)
        except DatabaseError:
            raise
        except Exception as exc:
            raise _translate_error(exc, self.backend) from exc
        self._last_cursor = cur
        return cur

    def executescript(self, script: str):
        """
        اجرای چند دستور پشت‌سرهم (معادل ``sqlite3.Connection.executescript``).

        مسیرها (E.4.7):
          • **SQLite:** همان ``sqlite3.Connection.executescript`` — بدون تغییر
            (اسکریپت را دست‌نخورده به درایور می‌دهد؛ ``?`` این‌جا معنا ندارد).
          • **PostgreSQL:** اسکریپت با :func:`core.db.scripts.split_sql_statements`
            به statementها می‌شکند و هر کدام با ``execute`` جداگانه اجرا می‌شود.
            چرا نه یک ``execute`` چنددستوری؟ چون psycopg فقط در پروتکل ساده قبول
            می‌کند: ``execute("A; B", (1,))`` ⇒
            ``cannot insert multiple commands into a prepared statement``
            (اندازه‌گیری‌شده). با اجرای جدا، خطا هم به همان statement نسبت داده
            می‌شود.

        قرارداد: مثل قبل، **هیچ commitی** این‌جا نیست (تراکنش در اختیار فراخوان).
        مقدار بازگشتی روی SQLite همان cursor درایور و روی PG cursor آخرین
        statement است (برای اسکریپت خالی/فقط‌کامنت: ``None``).

        Raises:
            ValueError: اسکریپت نامعتبر (رشته‌ی بسته‌نشده، dollar-quoting).
        """
        if self.backend == BACKEND_SQLITE:
            try:
                cur = self._conn.executescript(self._sql(script))
            except DatabaseError:
                raise
            except Exception as exc:
                raise _translate_error(exc, self.backend) from exc
            self._last_cursor = cur
            return cur

        cur = None
        for statement in split_sql_statements(script):
            cur = self.execute(statement)      # ترجمهٔ خطا از همین مسیر می‌آید
        self._last_cursor = cur
        return cur

    # ─── درج + شناسه (E.4.3) ────────────────────────────────────
    def json_extract(self, column: str, key: str, *, as_number: bool = False) -> str:
        """
        قطعهٔ SQL برای خواندن یک کلید از یک ستون JSON — مستقل از backend (E.4.9).

        چرا لازم است: ``json_extract(col, '$.key')`` تابع SQLite است (افزونهٔ
        JSON1) و روی PostgreSQL وجود ندارد —
        ``function json_extract(text, unknown) does not exist`` (سنجیده‌شده،
        E.4.6/E.4.9). معادل PostgreSQL همان عملگر ``->>`` است.

        تفاوت **نوع** که با ``as_number`` پوشش داده می‌شود:
          • SQLite: ``json_extract`` مقدار JSON را با نوع خودش برمی‌گرداند
            (عدد ⇒ INTEGER/REAL، رشته ⇒ TEXT)؛ همین باعث می‌شود مقایسهٔ
            ``= ?`` با یک عدد پایتونی کار کند؛
          • PostgreSQL: ``->>`` همیشه **متن** می‌دهد، پس برای مقایسهٔ عددی باید
            صریح cast شود. ``as_number=True`` روی PG همان cast را اضافه می‌کند
            (``::numeric``: هم عدد صحیح، هم اعشاری) و روی SQLite هیچ چیزی اضافه
            نمی‌کند، چون نوع از قبل درست است — یعنی SQLite بایت‌به‌بایت همان
            امروز می‌ماند.

        نکتهٔ دوم مخصوص این پروژه: cast به ``jsonb`` همچنان در قطعه می‌ماند، چون
        **همه** ستون‌های JSON پروژه روی PG متنی‌اند **جز** ``logs.details`` که از
        migration ``0004`` (فاز E.6.4) نوع ``jsonb`` دارد. برای ستون متنی، cast
        لازم است (وگرنه PG می‌گوید «operator does not exist: text ->> unknown» —
        سنجیده‌شده، E.4.9) و برای ستون ``jsonb`` بی‌هزینه است (``x::jsonb`` روی
        ``jsonb`` خودِ همان مقدار است؛ سنجیده‌شده در E.6.4: هر دو نوشتار یک عدد
        برمی‌گردانند). تفاوت پیامدِ دادهٔ خراب: روی ``jsonb`` اعتبارسنجی در **زمان
        نوشتن** می‌افتد (``22P02`` همان‌جا که مقدار نوشته می‌شود)، و روی ستون متنی
        در **زمان خواندن** — یعنی یک ردیف بدفرمت کل کوئری aggregate را می‌شکند.

        نکتهٔ ریز (ثبت‌شده، بی‌اثر روی دادهٔ فعلی): اگر مقدار JSON یک عدد اعشاری
        باشد، ``CAST(... AS INTEGER)`` در SQLite به سمت صفر می‌بُرد و در PG گرد
        می‌کند. شمارنده‌های صفحه در این پروژه همیشه صحیح‌اند
        (``json.dumps`` روی ``int``)، پس تفاوت عملی ندارد.

        Args:
            column: نام ستون (نقل‌قول می‌شود).
            key: کلید داخل JSON (بدون ``$.``).
            as_number: برای مقایسه‌های عددی روی PG.

        Returns:
            قطعهٔ SQL آمادهٔ درج در کوئری.

        Raises:
            ValueError: اگر ``key`` شناسهٔ ساده نباشد (مرز امنیتی: این متن SQL می‌سازد).
        """
        if not isinstance(key, str) or not _JSON_KEY_RE.match(key):
            raise ValueError(
                f"کلید JSON نامعتبر است: {key!r} (فقط شناسهٔ ساده مجاز است)")
        col = quote_ident(column)
        if self.backend == BACKEND_POSTGRESQL:
            # ستون JSON این پروژه روی PG از نوع **TEXT** است (migration ``0001``:
            # ``sa.Text``) و عملگر ``->>`` فقط روی ``json``/``jsonb`` تعریف شده؛
            # بدون cast، PG می‌گوید «operator does not exist: text ->> unknown»
            # (سنجیده‌شده، E.4.9). پس صریح cast می‌شود.
            jsonb = f"{col}::jsonb"
            fragment = f"({jsonb})->>'{key}'"
            return f"({fragment})::numeric" if as_number else fragment
        return f"json_extract({col}, '$.{key}')"


    def insert_returning_id(self, sql: str, params: Sequence[Any] = ()) -> Optional[int]:
        """
        یک ``INSERT`` اجرا می‌کند و شناسه‌ی ردیف درج‌شده را برمی‌گرداند.

        چرا متد جدا؟ ``lastrowid`` مفهومِ SQLite است و روی PostgreSQL وجود ندارد
        (اندازه‌گیری‌شده: نه ``psycopg.Connection`` و نه ``psycopg.Cursor`` چنین
        ویژگی‌ای ندارند). جایگزین استاندارد PG ``INSERT … RETURNING id`` است.
        این متد تفاوت را **یک‌جا** پنهان می‌کند تا کد دامنه دو نسخه‌ی موازی نداشته باشد:

          • SQLite: ``execute(sql, params)`` سپس ``cursor.lastrowid``؛
          • PostgreSQL: ``execute(sql.rstrip().rstrip(';') + " RETURNING id")``
            سپس ``fetchone()[0]``.

        قرارداد:
          • هیچ commitی اینجا انجام نمی‌شود (مثل ``execute``)؛ تراکنش در اختیار
            فراخوان است (یا ``commit=True`` در ``db_connection``)؛
          • ``None`` وقتی درج **هیچ ردیفی** ننوشته باشد (``rowcount == 0``). این
            مهم است چون ``sqlite3`` در آن حالت ``lastrowid`` را دست‌نخورده
            (مقدار درج قبلی) باقی می‌گذارد و بی‌صدا id اشتباه می‌داد؛
          • اگر SQL از قبل ``RETURNING`` داشته باشد ``ValueError`` با پیام روشن
            می‌دهیم — وگرنه روی PG خطای نحوی و روی SQLite id اشتباه می‌گرفتیم؛
          • ``lastrowid`` روی PG همچنان ``NotImplementedError`` می‌دهد (E.2): این
            متد راه درست را می‌دهد، ولی مسیر غلط را بی‌صدا نمی‌کند.

        Raises:
            ValueError: اگر ``sql`` از قبل ``RETURNING`` داشته باشد.
            NotImplementedError: هرگز — این متد روی هر دو backend کار می‌کند.
        """
        if "returning" in sql.lower():
            raise ValueError(
                "insert_returning_id خودش «RETURNING id» را اضافه می‌کند؛ "
                "SQL ورودی نباید RETURNING داشته باشد.")

        if self.backend == BACKEND_SQLITE:
            cur = self.execute(sql, params)
            if cur.rowcount == 0:
                return None
            return cur.lastrowid

        trimmed = sql.rstrip().rstrip(";").rstrip()
        cur = self.execute(f"{trimmed} RETURNING id", params)
        row = cur.fetchone()
        return None if row is None else int(row[0])

    # ─── مدیریت تراکنش/اتصال ────────────────────────────────────
    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()
        self._closed = True

    @property
    def closed(self) -> bool:
        """آیا ``close()`` صدا زده شده است؟ (جاروی تست؛ روی API برنامه اثری ندارد)"""
        return self._closed

    @property
    def raw_connection(self):
        """اتصال خام زیرین — برای جست‌وجو/تست و پل‌های موقت."""
        return self._conn

    def cursor(self):
        """
        cursor خام برای جاهایی که بازگشت مستقیم cursor لازم است.

        روی هر دو backend همان cursor درایور برگردانده می‌شود: ``sqlite3.Cursor``
        یا ``psycopg.Cursor``. کدی که روی این cursor کار می‌کند باید فقط از
        APIهای مشترک (``execute``/``fetchone``/``fetchall``/``rowcount``/
        ``description``) استفاده کند — نه ``lastrowid`` (روی PG وجود ندارد).
        """
        return self._conn.cursor()

    # ─── دسترسی‌های پاس‌ترو (کد فعلی روی اتصال خام از این‌ها استفاده می‌کند) ───
    def fetchone(self):
        return self._require_cursor("fetchone").fetchone()

    def fetchall(self):
        return self._require_cursor("fetchall").fetchall()

    def fetchmany(self, size: Optional[int] = None):
        cur = self._require_cursor("fetchmany")
        return cur.fetchmany() if size is None else cur.fetchmany(size)

    @property
    def lastrowid(self):
        """
        شناسه‌ی آخرین درج — **فقط SQLite**.

        روی PostgreSQL این مفهوم وجود ندارد (psycopg هم ``Cursor.lastrowid`` ندارد —
        اندازه‌گیری‌شده). به‌جای AttributeError گنگ، خطای صریح با راه‌حل درست می‌دهیم.
        از E.4.3 راه‌حل **پیاده‌شده** است: :meth:`insert_returning_id` (روی هر دو
        backend) و تنها مصرف زنده‌ی ``lastrowid`` در ``core/database.py``
        (``create_user``) به همان منتقل شد (``docs/postgresql-strictness.md`` بند ۹).
        این ویژگی فقط برای کدِ جامانده نگه داشته شده تا لو برود، نه اینکه بی‌صدا
        اشتباه کند.

        Raises:
            NotImplementedError: اگر backend ``postgresql`` باشد.
        """
        if self.backend != BACKEND_SQLITE:
            raise NotImplementedError(
                "lastrowid روی PostgreSQL وجود ندارد؛ از "
                "«INSERT … RETURNING id» + fetchone()[0] استفاده کنید "
                "(الزام E.4 — docs/postgresql-strictness.md بند ۹).")
        return self._require_cursor("lastrowid").lastrowid

    # ─── سازگاری سبک sqlite3 روی مسیر PG (E.2.3) ─────────────────
    @property
    def row_factory(self):
        """
        ``row_factory`` سبک sqlite3 — روی SQLite عبور داده می‌شود، روی PG بی‌اثر است.

        روی PostgreSQL، شکل ردیف با ``psycopg.rows`` تعیین می‌شود و در فاز E.5
        (``dict_row``) تصمیم‌گیری می‌شود؛ تا آن زمان ست‌کردن این ویژگی نه خطا
        می‌دهد و نه اثری دارد (خواندنش ``None`` است) تا کدی که برای sqlite3 نوشته
        شده روی PG ناگهان نشکند و رفتارش هم اشتباه فرض نشود.
        """
        if self.backend == BACKEND_SQLITE:
            return getattr(self._conn, "row_factory", None)
        return None

    @row_factory.setter
    def row_factory(self, value):
        if self.backend == BACKEND_SQLITE:
            self._conn.row_factory = value
        # postgresql: عمداً بی‌اثر (مستند در docstring بالا)

    @property
    def description(self):
        cur = self._last_cursor
        return None if cur is None else cur.description

    @property
    def rowcount(self):
        cur = self._last_cursor
        return -1 if cur is None else cur.rowcount

    def _require_cursor(self, what: str):
        cur = self._last_cursor
        if cur is None:
            raise self._programming_error(
                f"no active cursor: execute() را قبل از {what} صدا بزنید")
        return cur

    def _programming_error(self, message: str) -> Exception:
        """
        خطای «استفاده‌ی نادرست از API» با نوع مناسبِ backend.

        SQLite ⇒ ``sqlite3.ProgrammingError`` (بدون تغییر نسبت به فاز D، تا
        ``except``های موجود دست‌نخورده کار کنند). PostgreSQL ⇒
        ``psycopg.ProgrammingError`` (همان خانواده‌ی خطا در درایور مقصد).
        """
        if self.backend == BACKEND_SQLITE:
            return sqlite3.ProgrammingError(message)
        try:
            import psycopg  # noqa: PLC0415 — تنبل، مثل connect_postgres
            return psycopg.ProgrammingError(message)
        except ImportError:  # pragma: no cover - مسیر دفاعی
            return DatabaseConnectionError(message, backend=self.backend)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._require_cursor("iteration"))

    # ─── context manager ────────────────────────────────────────
    def __enter__(self) -> "ConnectionAdapter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        کامیت یا rollback — با **همان** سیاست قبلی ``db_connection``:
        ``db_connection`` خودش هم commit/rollback می‌کند (هر دو idempotent‌اند)،
        ولی اگر کسی adapter را دستی بسازد و داخل ``with`` خطا رخ دهد، اینجا
        rollback می‌شود.
        """
        if exc_type is None:
            self.commit()
        else:
            with contextlib.suppress(Exception):
                self.rollback()
        return False  # خطا را سرکوب نمی‌کنیم

    def __repr__(self) -> str:  # pragma: no cover - فقط برای دیباگ
        state = "closed" if self._closed else "open"
        return f"<ConnectionAdapter backend={self.backend} {state}>"


def connect_sqlite(path: str, timeout: float = 30.0) -> ConnectionAdapter:
    """
    ساخت اتصال SQLite و پیچیدن آن در adapter.

    **تنها جای مجاز ``sqlite3.connect`` در کد برنامه** (معیار پذیرش فاز D).
    ``PRAGMA busy_timeout`` برابر همان ``timeout`` ست می‌شود؛ این دقیقاً کاری است
    که پارامتر ``timeout`` خودِ sqlite3 هم انجام می‌دهد (busy_timeout در سطح C)،
    پس نه رفتار جدیدی اضافه می‌کند و نه چیزی را عوض می‌کند.

    Raises:
        DatabaseConnectionError: اگر ساخت اتصال شکست بخورد.
    """
    try:
        raw = sqlite3.connect(path, timeout=timeout)
    except Exception as exc:
        raise DatabaseConnectionError(
            f"اتصال به دیتابیس ممکن نشد (backend={BACKEND_SQLITE}, path={path}): {exc}",
            backend=BACKEND_SQLITE) from exc
    try:
        raw.execute(f"PRAGMA busy_timeout={int(float(timeout) * 1000)}")
    except Exception:  # pragma: no cover - دفاعی
        # فاز E.4.4: عمداً Exception (نه sqlite3.Error) — این PRAGMA فقط یک
        # بهینه‌سازی است و نباید ساخت اتصال را بشکند؛ همچنین در پایان E.4.4 هیچ
        # ``except sqlite3.*`` در core/ و web/ نمی‌ماند (الزام مالک).
        pass
    return ConnectionAdapter(raw, backend=BACKEND_SQLITE)


# ─────────────────────────────────────────────────────────────
# PostgreSQL (فاز E.2)
# ─────────────────────────────────────────────────────────────
#: قفل زمان‌نشست روی UTC (بند ۷ `docs/postgresql-strictness.md`): هم‌ارز
#: `SET TIME ZONE 'UTC'`، ولی **قبل از اتصال** و در سطح libpq ⇒ هیچ تراکنش/دستور
#: اضافه‌ای روی اتصال اجرا نمی‌شود.
TZ_OPTIONS = "-c timezone=UTC"


def _tokenize_conninfo(dsn: str) -> list:
    """
    رشته‌ی conninfo سبک libpq را به توکن‌های ``key=value`` می‌شکند.

    چرا لازم است: مقدارهایی مثل ``options='-c timezone=UTC -c a=1'`` فاصله دارند؛
    ``str.split()`` آن‌ها را به هم می‌ریزد. این تابع نقل‌قول تک‌کوت را (با فرار
    ``\\'``) می‌فهمد. برای DSN از نوع URI (``postgresql://…``) توکنی برنمی‌گرداند
    (چون ``=`` ندارد/نامعتبر است) و فراخوان از مسیر URI استفاده می‌کند.
    """
    tokens, buf, i, n, in_quote = [], [], 0, len(dsn), False
    while i < n:
        ch = dsn[i]
        if in_quote:
            if ch == "\\" and i + 1 < n:
                buf.append(dsn[i:i + 2]); i += 2; continue
            if ch == "'":
                in_quote = False; i += 1; continue
            buf.append(ch); i += 1; continue
        if ch == "'":
            in_quote = True; i += 1; continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf)); buf = []
            i += 1; continue
        buf.append(ch); i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _conninfo_value(dsn: str, key: str) -> Optional[str]:
    """مقدار یک کلید conninfo (بدون نقل‌قول)؛ نبود کلید ⇒ ``None``."""
    if "://" in dsn:  # URI: این تجزیه معنا ندارد
        return None
    for token in _tokenize_conninfo(dsn):
        k, sep, v = token.partition("=")
        if sep and k.strip().lower() == key.lower():
            return v.strip().strip("'\"") if v.strip().startswith(("'", '"')) else v.strip()
    return None


def timezone_options(dsn: str) -> str:
    """
    مقدار پارامتر ``options`` برای قفل‌کردن UTC — بدون از دست دادن تنظیمات موجود.

    اگر خودِ DSN ``options`` داشته باشد، به انتهای آن اضافه می‌شود (در libpq
    ``-c``‌های بعدی بر قبلی غالب‌اند ⇒ UTC تضمین می‌شود) وگرنه فقط UTC.
    """
    existing = _conninfo_value(dsn, "options")
    if not existing:
        return TZ_OPTIONS
    if "timezone=" in existing.lower().replace(" ", ""):
        return existing + " " + TZ_OPTIONS
    return existing + " " + TZ_OPTIONS


def _import_psycopg():
    """
    درایور psycopg ۳ را import می‌کند (نه psycopg2).

    Raises:
        DatabaseConnectionError: اگر نصب نباشد — با پیام روشن نصب، چون این خطا
            «پیکربندی» نیست بلکه «وابستگی غایب» است.
    """
    try:
        import psycopg  # noqa: PLC0415 — عمدی: import تنبل تا sqlite به psycopg وابسته نشود
        return psycopg
    except ImportError as exc:
        raise DatabaseConnectionError(
            "درایور psycopg (نسخه‌ی ۳) نصب نیست؛ برای backend=postgresql لازم است. "
            "نصب: python -m pip install \"psycopg[binary]\"",
            backend=BACKEND_POSTGRESQL) from exc


def connect_postgres(dsn: Optional[str] = None, timeout: float = 30.0) -> ConnectionAdapter:
    """
    ساخت اتصال PostgreSQL و پیچیدن آن در adapter.

    Args:
        dsn: نشانی اتصال سبک libpq؛ ``None`` ⇒ از ``PG_DSN`` خوانده می‌شود.
        timeout: مهلت اتصال (ثانیه) ⇒ ``connect_timeout`` libpq (حداقل ۱ ثانیه).

    قرارداد اتصال: ``autocommit=False`` (پیش‌فرض psycopg ⇒ هر commit صریح، مثل
    SQLite)، ``options=-c timezone=UTC`` (قفل UTC در سطح session)، و
    ``connect_timeout`` همان ``timeout`` adapter.

    Raises:
        ConfigurationError: نبود DSN (نه در پارامتر، نه در ``PG_DSN``).
        DatabaseConnectionError: نبود درایور، یا شکست اتصال (سرور خاموش، رمز/DB غلط).
    """
    target = resolve_dsn(BACKEND_POSTGRESQL, dsn)   # خالی ⇒ ConfigurationError
    psycopg = _import_psycopg()
    try:
        raw = psycopg.connect(
            target,
            connect_timeout=max(1, int(float(timeout))),
            autocommit=False,
            options=timezone_options(target),
        )
    except Exception as exc:
        raise DatabaseConnectionError(
            f"اتصال به PostgreSQL ممکن نشد (backend={BACKEND_POSTGRESQL}): "
            f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}",
            backend=BACKEND_POSTGRESQL) from exc
    return ConnectionAdapter(raw, backend=BACKEND_POSTGRESQL)


def connect(path: Optional[str] = None, *, timeout: float = 30.0,
            backend: Optional[str] = None) -> ConnectionAdapter:
    """
    نقطه‌ی ورود ساخت اتصال: انتخاب backend + مسیر پیش‌فرض.

    Args:
        path: مسیر صریح فایل دیتابیس **فقط برای sqlite**؛ اگر ``None`` باشد
            :func:`default_db_path` (یعنی ``config.settings.DB_PATH``) در همین لحظه
            خوانده می‌شود. برای postgresql، مسیر فایل معنا ندارد و نشانی از
            ``PG_DSN`` می‌آید (دادن ``path`` همراه postgresql خطای صریح است).
        timeout: مهلت قفل‌شدن برای sqlite / مهلت اتصال برای postgresql (ثانیه).
        backend: اگر ``None`` باشد از ``DB_BACKEND`` خوانده می‌شود.

    Raises:
        UnsupportedBackendError: backend ناشناخته — بدون fallback بی‌صدا.
        ConfigurationError: ``postgresql`` بدون DSN (از :func:`resolve_dsn`)، یا
            دادن ``path`` همراه postgresql.
        DatabaseConnectionError: شکست ساخت اتصال.
    """
    resolved = resolve_backend(backend)
    if resolved == BACKEND_SQLITE:
        return connect_sqlite(path if path is not None else default_db_path(), timeout)
    if resolved == BACKEND_POSTGRESQL:
        if path is not None:
            raise ConfigurationError(
                f"مسیر فایل («{path}») فقط برای backend={BACKEND_SQLITE} معنا دارد؛ "
                f"برای {BACKEND_POSTGRESQL} نشانی را در {ENV_PG_DSN} بگذارید.",
                backend=resolved)
        return connect_postgres(None, timeout)
    raise UnsupportedBackendError(  # نگهبان دفاعی: با SUPPORTED_BACKENDS ناهم‌خوان
        f"backend «{resolved}» مسیر اتصالی در connect() ندارد.", backend=resolved)


__all__ = [
    "ConnectionAdapter",
    "connect",
    "connect_sqlite",
    "connect_postgres",
    "default_db_path",
    "resolve_backend",
    "resolve_dsn",
    "dsn_from_env",
    "timezone_options",
    "TZ_OPTIONS",
    "BACKEND_ALIASES",
    "BACKEND_SQLITE",
    "BACKEND_POSTGRESQL",
    "SUPPORTED_BACKENDS",
    "ALL_BACKENDS",
    "ENV_DB_BACKEND",
    "ENV_PG_DSN",
]
