# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_collation_ordering.py
# @brief:        Tests text ordering equivalence between SQLite and PostgreSQL (E.6.3).
# @details:      Responsibilities: Check collation-sensitive ordering on both backends.
#                Main API: SqliteBaselineOrderTests, PostgresOrderingEquivalenceTests
#                Side effects: database access, file I/O
# @dependencies: external: psycopg
# @notes:        Reads environment variables: PG_DSN. Can be run directly as a script.
# =============================================================================

r"""
Tests E.6.3 — Text Order Equivalence: `C' on PG vs. `BINARY' on SQLite.

Background to the decision (evidence, not guesswork)
----------------------------
The phase E question was whether PG ordering should be changed from `C' (byte/codepoint comparison) to
``fa-IR-x-icu'' (Persian alphabet) should be changed. Measured on the same server (PG 16.15, ICU
built, `datcollate = C`) with 18 real Farsi/Latin/numerical names:

* **15 rows out of 18** were changing their places ⇒ **not insignificant** difference;
* Under `C', the order is exactly the same as Python's `sorted()' and SQLite's `COLLATE BINARY' results
  (this test locks the same);
* Under ``fa-IR-x-icu'' the Persian numeral comes next to the Latin numeral (`2 stars' from the last to the second) and
  ``admin office'' **before** ``Admin Office'' — means even the Latin mode is moved;
* `=` remains case sensitive (``ADMIN OFFICE'' = 'admin office'` under both
  collation is incorrect) ⇒ ICU is not the place to solve the NOCASE problem.

Decision: **`C` remains.**
The reason is not "the effect of the difference is small" (the effect is large); The reason is that in the parallel period and
rollback, "same layout on two backends" is more important than "prettier layout on one backend".
— and SQLite has no ICU (`PRAGMA compile_options' has no ICU option ⇒ same way
SQLite does not exist with ICU). However, the program's textual ORDER BYs only on `printer_ip'
and `username` (ASCII) and `lower(username)`, and the display Persian order in the UI with
``localeCompare'' is done by the browser, so changing the collation does not have any immediate user benefit.

If one day the order of Persian alphabet in SQL becomes necessary: it is not necessary to change the whole schema - the same
per-query ``ORDER BY col COLLATE "fa-IR-x-icu"'' is sufficient (or this-index with the same
collation); This row is registered as TD-013 at docs/tech-debt.md.

Isolation note: everything is placed on the temp table in its own connection; Not schema
The application crashes and not `logs.db`.
"""
import os
import sqlite3
import unittest

FIXTURE = [
    "رضا محمدی", "پارسه چاپگر", "ثابت کار", "Admin Office", "admin office",
    "2ستاره", "۲ستاره", "چاپگر شبکه", "بیضی پرینتر", "گسترش اداری",
]

# codepoint order = "expect"; If someone changes the fixture, both lists should be together
# be updated (this is a duplicate on purpose so that the test has an independent claim).
EXPECTED_CODEPOINT_ORDER = [
    "2ستاره", "Admin Office", "admin office", "بیضی پرینتر", "ثابت کار",
    "رضا محمدی", "پارسه چاپگر", "چاپگر شبکه", "گسترش اداری", "۲ستاره",
]

_PG_DSN = (os.getenv("PG_DSN", "") or "").strip()


def _pg_connect():
    """Helper: pg connect."""
    import psycopg
    return psycopg.connect(_PG_DSN, connect_timeout=3)


def _pg_available():
    """Helper: pg available."""
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
    """Is `fa-IR-x-icu` on this server? (ICU is not in the old build of PGs.)"""
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
    """The SQLite side: BINARY is the codepoint — it's the decision anchor, not the new assertion."""

    def test_binary_order_is_codepoint_order(self):
        """Verifies binary order is codepoint order."""
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
        If SQLite is ever built with ICU, "Stay C" will be revised; This is the bell test
        Change is (if ICU comes, order can be matched in two backends).
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
    """PG with its default collation (`C`) should give exactly the same collation as SQLite."""

    def _order(self, conn, expr):
        # `executemany' is the *cursor* method (not on Connection in psycopg 3) — and expr
        # It comes from this file, not user input.
        """Helper: order."""
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE coll_probe (s text) ON COMMIT DROP")
            cur.executemany("INSERT INTO coll_probe VALUES (%s)", [(x,) for x in FIXTURE])
            cur.execute(f"SELECT s FROM coll_probe ORDER BY {expr}")
            return [r[0] for r in cur.fetchall()]

    def test_database_collation_is_c(self):
        """The fact that the database collation is `C' is the basis for this decision - so it locks."""
        with _pg_connect() as conn:
            row = conn.execute(
                "SELECT datcollate FROM pg_database WHERE datname = current_database()").fetchone()
        self.assertEqual(row[0], "C",
                         "collation دیتابیس عوض شده؛ ترتیب متن (و این تست) باید بازبینی شود")

    def test_default_order_matches_sqlite_binary(self):
        """Verifies default order matches sqlite binary."""
        with _pg_connect() as conn:
            self.assertEqual(self._order(conn, "s"), EXPECTED_CODEPOINT_ORDER,
                             "ترتیب پیش‌فرض PG از SQLite BINARY جدا شد — واگرایی خاموش")

    @unittest.skipUnless(_icu_fa_available(), "collation fa-IR-x-icu روی این سرور نیست")
    def test_icu_fa_order_would_diverge_and_is_rejected(self):
        """
        A document that “Why not ICU” measures two specific shifts, if ever
        Someone reverted the decision by changing the overall collation, know what exactly changed
        (This test does not claim that ICU is bad; it says that it is **divergent**).
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

    # ORDER BY on text column: Today only ASCII identifiers (IP/username). This list
    # It is a "witness", not a confirmation: if a new location is added, the test turns red
    # Author should see decision E.6.3 (COLLATE should be explicit for Persian order).
    ALLOWED_TEXT_ORDER_SITES = {
        "core/database.py": "ORDER BY printer_ip — شناسهٔ ASCII",
        "core/yield_engine.py": "ORDER BY printer_ip, color — شناسهٔ ASCII",
        "web/routes/metrics.py": "ORDER BY printer_ip — شناسهٔ ASCII",
        "web/routes/import_db.py": "ORDER BY {_q(ip_col)} — شناسهٔ ASCII (فایل picker)",
    }
    TEXT_TOKENS = ("USERNAME", "PRINTER_IP", "IP_COL", "NAME", "TITLE")

    def test_text_order_by_sites_are_only_ascii_identifiers(self):
        """Verifies text order by sites are only ascii identifiers."""
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
                                # The separator must be the same as the ALLOWED_TEXT_ORDER_SITES keys
                                # (``/``); On Windows ``os.path.relpath'' gives a backslash.
                                found.add(os.path.relpath(path, root).replace(os.sep, "/"))
        self.assertEqual(found, set(self.ALLOWED_TEXT_ORDER_SITES),
                         "محل تازه‌ای ORDER BY متنی دارد؛ برای ترتیب عمدی باید "
                         "COLLATE صریح بگیری یا تصمیم E.6.3 را بازبینی کنی")

    def test_allowed_sites_are_still_the_documented_ones(self):
        """If one of these four locations is deleted/overwritten, the list above should also be updated."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel, _reason in self.ALLOWED_TEXT_ORDER_SITES.items():
            self.assertTrue(os.path.exists(os.path.join(root, rel)), f"{rel} دیگر نیست")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
