# -*- coding: utf-8 -*-
r"""
تست‌های E.6.3 — هم‌ارزی ترتیب متن: `C` روی PG در برابر `BINARY` روی SQLite.

پیشینۀ تصمیم (شواهد، نه حدس)
----------------------------
سؤال فاز E این بود که آیا ترتیب‌دهی PG باید از `C` (مقایسهٔ بایت/کدپوینت) به
`fa-IR-x-icu` (الفبای فارسی) تغییر کند. اندازه‌گیری روی همین سرور (PG 16.15، ICU
ساخته‌شده، `datcollate = C`) با ۱۸ نام واقعیِ فارسی/لاتین/رقمی:

* **۱۵ ردیف از ۱۸** جای خود را عوض می‌کردند ⇒ تفاوت «ناچیز» **نیست**؛
* زیر `C` ترتیب دقیقاً همان `sorted()` پایتون و `COLLATE BINARY` نتایج SQLite است
  (این تست همان را قفل می‌کند)؛
* زیر `fa-IR-x-icu` رقم فارسی کنار رقم لاتین می‌آید (`۲ستاره` از آخر به دوم) و
  `admin office` **پیش از** `Admin Office` — یعنی حتی حالت لاتین هم جابه‌جا می‌شود؛
* `=` همچنان به‌بزرگی‌حساس می‌ماند (`'ADMIN OFFICE' = 'admin office'` زیر هر دو
  collation نادرست است) ⇒ ICU جای حل مسئلۀ NOCASE نیست.

تصمیم: **`C` می‌ماند.**
دلیل، «کم‌اثر بودن تفاوت» نیست (اثر زیاد است)؛ دلیل این است که در دورۀ موازی و
rollback، «ترتیب یکسان روی دو backend» از «ترتیب زیباتر روی یک backend» مهم‌تر است
— و SQLite ICU ندارد (`PRAGMA compile_options` هیچ گزینهٔ ICU ندارد ⇒ راهِ هم‌سو
کردن SQLite با ICU وجود ندارد). هرچند ORDER BYهای متنی برنامه فقط روی `printer_ip`
و `username` (ASCII) و `lower(username)` است و ترتیب فارسیِ نمایشی در UI با
`localeCompare` مرورگر انجام می‌شود، پس تغییر collation سود کاربریِ فوری هم ندارد.

اگر روزی ترتیب الفبای فارسی در SQL لازم شد: لازم نیست کل schema عوض شود — همان
per-query ``ORDER BY col COLLATE "fa-IR-x-icu"`` کافی است (یا this-index با همان
collation)؛ این ردیف به‌عنوان TD-013 در docs/tech-debt.md ثبت شده است.

نکتهٔ ایزولاسیون: همه‌چیز روی temp table در connection خودش می‌گذارد؛ نه schema
برنامه دست می‌خورد و نه `logs.db`.
"""
import os
import sqlite3
import unittest

FIXTURE = [
    "رضا محمدی", "پارسه چاپگر", "ثابت کار", "Admin Office", "admin office",
    "2ستاره", "۲ستاره", "چاپگر شبکه", "بیضی پرینتر", "گسترش اداری",
]

# ترتیب کدپوینتی = «انتظار»؛ اگر کسی fixture را عوض کند، هر دو فهرست باید با هم
# به‌روز شوند (این عمداً duplicate است تا تست، ادعای مستقل داشته باشد).
EXPECTED_CODEPOINT_ORDER = [
    "2ستاره", "Admin Office", "admin office", "بیضی پرینتر", "ثابت کار",
    "رضا محمدی", "پارسه چاپگر", "چاپگر شبکه", "گسترش اداری", "۲ستاره",
]

_PG_DSN = (os.getenv("PG_DSN", "") or "").strip()


def _pg_connect():
    import psycopg
    return psycopg.connect(_PG_DSN, connect_timeout=3)


def _pg_available():
    if not _PG_DSN:
        return False, "PG_DSN ست نشده ⇒ قیاس ترتیب روی PG انجام نشد"
    try:
        with _pg_connect() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        return False, f"PG در دسترس نیست ({type(exc).__name__}) ⇒ قیاس ترتیب انجام نشد"
    return True, None


_PG_OK, _PG_SKIP = _pg_available()

_HAS_ICU_FA = None


def _icu_fa_available():
    """`fa-IR-x-icu` روی این سرور هست؟ (ICU در build قدیمی PGها نیست.)"""
    global _HAS_ICU_FA
    if _HAS_ICU_FA is None:
        try:
            with _pg_connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM pg_collation WHERE collname = 'fa-IR-x-icu'").fetchone()
            _HAS_ICU_FA = row is not None
        except Exception:  # noqa: BLE001
            _HAS_ICU_FA = False
    return _HAS_ICU_FA


class SqliteBaselineOrderTests(unittest.TestCase):
    """سمت SQLite: BINARY همان کدپوینت است — این لنگرِ تصمیم است، نه ادعای جدید."""

    def test_binary_order_is_codepoint_order(self):
        self.assertEqual(EXPECTED_CODEPOINT_ORDER, sorted(FIXTURE),
                         "فهرست انتظار باید همان sort پایتون (کدپوینت) باشد")
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE t (s text)")
            conn.executemany("INSERT INTO t VALUES (?)", [(x,) for x in FIXTURE])
            rows = [r[0] for r in conn.execute("SELECT s FROM t ORDER BY s COLLATE BINARY")]
        finally:
            conn.close()
        self.assertEqual(rows, EXPECTED_CODEPOINT_ORDER)

    def test_sqlite_build_has_no_icu(self):
        """
        اگر روزی SQLite با ICU ساخته شد، «C بماند» بازبینی می‌شود؛ این تست زنگِ
        تغییر است (اگر ICU بیاید، ترتیب را می‌توان در دو backend هم‌سان کرد).
        """
        conn = sqlite3.connect(":memory:")
        try:
            opts = [r[0] for r in conn.execute("PRAGMA compile_options")]
        finally:
            conn.close()
        self.assertFalse([o for o in opts if "ICU" in o.upper()],
                         f"SQLite این build ICU دارد ({opts}) ⇒ تصمیم E.6.3 را بازبینی کن")


@unittest.skipUnless(_PG_OK, _PG_SKIP or "")
class PostgresOrderingEquivalenceTests(unittest.TestCase):
    """PG با collation پیش‌فرضش (`C`) باید دقیقاً همان ترتیب SQLite را بدهد."""

    def _order(self, conn, expr):
        # `executemany` متد *cursor* است (روی Connection در psycopg 3 نیست) — و expr
        # از همین فایل می‌آید، نه ورودی کاربر.
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE coll_probe (s text) ON COMMIT DROP")
            cur.executemany("INSERT INTO coll_probe VALUES (%s)", [(x,) for x in FIXTURE])
            cur.execute(f"SELECT s FROM coll_probe ORDER BY {expr}")
            return [r[0] for r in cur.fetchall()]

    def test_database_collation_is_c(self):
        """`C` بودنِ collation دیتابیس، مبنای این تصمیم است — پس قفل می‌شود."""
        with _pg_connect() as conn:
            row = conn.execute(
                "SELECT datcollate FROM pg_database WHERE datname = current_database()").fetchone()
        self.assertEqual(row[0], "C",
                         "collation دیتابیس عوض شده؛ ترتیب متن (و این تست) باید بازبینی شود")

    def test_default_order_matches_sqlite_binary(self):
        with _pg_connect() as conn:
            self.assertEqual(self._order(conn, "s"), EXPECTED_CODEPOINT_ORDER,
                             "ترتیب پیش‌فرض PG از SQLite BINARY جدا شد — واگرایی خاموش")

    @unittest.skipUnless(_icu_fa_available(), "collation fa-IR-x-icu روی این سرور نیست")
    def test_icu_fa_order_would_diverge_and_is_rejected(self):
        """
        سندی که «چرا ICU نه»: دو جابه‌جاییِ مشخص را اندازه می‌گیرد، تا اگر روزی
        کسی تصمیم را با تغییر collation کلی برگرداند، بداند دقیقاً چه چیزی عوض
        می‌شود (این تست ادعا نمی‌کند ICU بد است؛ می‌گوید **واگرا** است).
        """
        with _pg_connect() as conn:
            fa = self._order(conn, 's COLLATE "fa-IR-x-icu"')
        self.assertNotEqual(fa, EXPECTED_CODEPOINT_ORDER)
        pos = {s: i for i, s in enumerate(fa)}
        c_pos = {s: i for i, s in enumerate(EXPECTED_CODEPOINT_ORDER)}
        self.assertLess(pos["پارسه چاپگر"], pos["ثابت کار"],
                        "الفبای فارسی: پ پس از ب و پیش از ت/ث")
        self.assertGreater(c_pos["پارسه چاپگر"], c_pos["ثابت کار"],
                           "زیر C برعکس است — همین جابه‌جایی، دلیل رد ICU است")
        self.assertLess(pos["admin office"], pos["Admin Office"],
                        "ICU حروف کوچک را پیش از بزرگ می‌گذارد؛ C برعکس")
        self.assertLess(pos["۲ستاره"], pos["2ستاره"] + 2,
                        "رقم فارسی زیر ICU کنار رقم لاتین می‌نشیند، نه آخرِ فهرست")

    # ORDER BY روی ستون متنی: امروز فقط شناسه‌های ASCII (IP/username). این فهرست
    # «شاهد» است، نه تأیید: اگر محلِ تازه‌ای اضافه شود، تست قرمز می‌شود تا
    # نویسنده تصمیم E.6.3 را ببیند (برای ترتیب فارسی باید COLLATE صریح گرفت).
    ALLOWED_TEXT_ORDER_SITES = {
        "core/database.py": "ORDER BY printer_ip — شناسهٔ ASCII",
        "core/yield_engine.py": "ORDER BY printer_ip, color — شناسهٔ ASCII",
        "web/routes/metrics.py": "ORDER BY printer_ip — شناسهٔ ASCII",
        "web/routes/import_db.py": "ORDER BY {_q(ip_col)} — شناسهٔ ASCII (فایل picker)",
    }
    TEXT_TOKENS = ("USERNAME", "PRINTER_IP", "IP_COL", "NAME", "TITLE")

    def test_text_order_by_sites_are_only_ascii_identifiers(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        found = set()
        for pkg in ("core", "web"):
            for dirpath, _dirs, files in os.walk(os.path.join(root, pkg)):
                for name in sorted(files):
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8") as fh:
                        for line in fh:
                            up = line.upper()
                            if "ORDER BY" not in up:
                                continue
                            tail = up.split("ORDER BY", 1)[1]
                            if any(tok in tail for tok in self.TEXT_TOKENS) \
                                    and "COLLATE" not in tail:
                                # جداکننده باید هم‌شکل کلیدهای ALLOWED_TEXT_ORDER_SITES باشد
                                # (``/``)؛ روی ویندوز ``os.path.relpath`` بک‌اسلش می‌دهد.
                                found.add(os.path.relpath(path, root).replace(os.sep, "/"))
        self.assertEqual(found, set(self.ALLOWED_TEXT_ORDER_SITES),
                         "محل تازه‌ای ORDER BY متنی دارد؛ برای ترتیب عمدی باید "
                         "COLLATE صریح بگیری یا تصمیم E.6.3 را بازبینی کنی")

    def test_allowed_sites_are_still_the_documented_ones(self):
        """اگر یکی از این چهار محل حذف/بازنویسی شد، فهرست بالا هم باید به‌روز شود."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel, _reason in self.ALLOWED_TEXT_ORDER_SITES.items():
            self.assertTrue(os.path.exists(os.path.join(root, rel)), f"{rel} دیگر نیست")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
