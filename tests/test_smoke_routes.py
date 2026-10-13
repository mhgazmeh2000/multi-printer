# -*- coding: utf-8 -*-
"""
Smoke test مسیرهای دارای SQL خام (فاز D — کار D.3).

چرا این فایل؟
    چهار مسیر زیر در فاز E باید روی PostgreSQL هم کار کنند. این تست‌ها یک تور
    ایمنیِ سریع‌اند تا اگر روزی اتصال/SQL این مسیرها بشکند، همان‌جا لو برود
    (نه در تست‌های عمیق دامنه).

مسیرها:
    POST /api/import/analyze     → اتصال به فایل DB موقت آپلودی (مسیر غیرِ پیش‌فرض)
    GET  /api/export/logs        → SELECT روی logs
    GET  /api/stats/daily        → SELECT/GROUP BY روی logs
    GET  /api/security/events    → SELECT روی security_events

هر تست سه ادعا دارد (کار E.0.6 سخت‌گیرش کرد):
    ۱) بدون ورود ⇒ **دقیقاً ۴۰۱** (پیش‌تر «۲۰۰ یا ۴۰۱» بود؛ اگر روزی مسیری
       ناخواسته باز شود، تستِ قدیمی سبز می‌ماند و کسی نمی‌فهمید)؛
    ۲) با ورود ادمین ⇒ ۲۰۰ **و نتیجه‌ی SQL واقعی** (نه فقط کد وضعیت). ارزش
       اصلی این تور این است که کوئری‌ها واقعاً اجرا شوند؛ چون پاسخ ناشناس ۴۰۱
       است و هیچ SQLی نمی‌بیند؛
    ۳) بدنه‌ی ۴۰۱ هیچ داده‌ی واقعی (IP، نام پرینتر، نوع رویداد) نشت نمی‌دهد.

نکته‌ی E.0.6: ``SEED_DAY`` دینامیک است (روز جاری)؛ پیش‌تر ``2026-10-01`` ثابت
بود و پس از ۳۰ روز، ادعای «۳ صفحه در آن روز» بی‌اثر می‌شد.

نکته‌ی E.3.7: از فاز E.3 داده‌ی ذخیره‌شده UTC/Z است (طول ثابت ۲۷) و سبد روزانه در
``/api/stats/daily`` روزِ **محلی** است ⇒ ``seed_ts()`` رشته را از ساعت محلی می‌سازد
تا mean «روز» و «لحظه» یکی باشد.

اجرا:
    python -m pytest tests/test_smoke_routes.py -q
    python -m unittest tests.test_smoke_routes -v
"""
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, time, timedelta

import pytest

from core.database import add_event, init_db
from core.time_utils import local_tz, to_utc_z
from core.security_audit import (
    SecurityEvent,
    Severity,
    init_security_audit,
    log_security_event,
)
from models import User
from web import create_app

SEED_IP = "192.0.2.42"
# روز seed دینامیک است (E.0.6): همیشه «امروز» ⇒ هم داخل پنجرهٔ آماری ۳۰ روزه
# می‌ماند، هم تست پس از گذشت زمان بی‌اثر نمی‌شود. یک مقدار واحد برای هر سه
# مصرف‌کننده (add_event، DB آپلودی، ادعای تجمیع روزانه) استفاده می‌شود.
#
# فاز E.3: داده‌ی DB استاندارد UTC/Z است و سبدِ آمار روزانه «روزِ محلی
# (Asia/Tehran)» است ⇒ روزِ seed و رشته‌ی زمانش باید از همان مبدأ ساخته شوند،
# وگرنه بین ساعت ۲۰:۳۰ تا ۲۴:۰۰ تهران (که روز UTC با روز محلی فرق دارد) تست
# ناپایدار می‌شد.
SEED_LOCAL_TZ = local_tz()
SEED_DAY = datetime.now(SEED_LOCAL_TZ).date().isoformat()


def seed_ts(hour: int = 10, minute: int = 5) -> str:
    """timestamp استاندارد (UTC/Z) برای ساعتِ محلیِ داده‌شده در «امروزِ محلی»."""
    day = datetime.now(SEED_LOCAL_TZ).date()
    return to_utc_z(datetime.combine(day, time(hour, minute), tzinfo=SEED_LOCAL_TZ))
SEED_PAGES = 3
# عبارت‌هایی که هرگز نباید در پاسخ ناشناس دیده شوند (تست عدم نشت)
LEAK_MARKERS = (SEED_IP, "Smoke Printer", "PRINT", "REFILL")


def _uploaded_db_bytes() -> bytes:
    """
    یک فایل ``.db`` واقعی (ساخته‌شده در فایل موقت، نه حافظه) با جدول logs.

    چرا فایل: مسیر ``/api/import/analyze`` با ``sqlite3.connect(path)`` روی
    فایلِ ذخیره‌شده کار می‌کند؛ با DB حافظه‌ای این مسیر قابل آزمودن نیست.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    raw = sqlite3.connect(path)
    try:
        raw.execute("""CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, printer_ip TEXT, printer_name TEXT,
            timestamp TEXT, type TEXT, message TEXT, pages INTEGER, color TEXT, code TEXT,
            severity TEXT, paper_size TEXT, username TEXT, details TEXT)""")
        raw.execute("INSERT INTO logs (printer_ip, printer_name, timestamp, type, pages) "
                    "VALUES (?, ?, ?, 'PRINT', 7)",
                    (SEED_IP, "Smoke Printer", seed_ts(10, 0)))
        raw.commit()
    finally:
        raw.close()
    try:
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)


class SmokeRoutesTests(unittest.TestCase):

    def setUp(self):
        # DB_PATH نسبی است ⇒ با chdir، هر تست DB موقت خودش را دارد.
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        init_security_audit()
        add_event(SEED_IP, "PRINT", {"timestamp": seed_ts(10, 5),
                                     "pages": SEED_PAGES, "message": "smoke"})
        log_security_event(SecurityEvent.SUCCESSFUL_LOGIN, Severity.INFO,
                           user_identifier="smoke_admin")
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.admin = User.create(username="smoke_admin", email="smoke@test.io",
                                 password="secret123", role="admin", is_verified=True)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _assert_anonymous_denied(self, resp, endpoint: str):
        """بدون ورود ⇒ **دقیقاً ۴۰۱** (نه ۲۰۰ و نه هیچ کد دیگری).

        اگر پاسخ ۲۰۰ باشد اول با پیام صریح fail می‌کند تا علت پیدا باشد؛
        سپس برابری دقیق با ۴۰۱ بررسی می‌شود.
        """
        if resp.status_code == 200:
            self.fail(f"نشت دسترسی: endpoint {endpoint} بدون auth پاسخ ۲۰۰ داد")
        self.assertEqual(resp.status_code, 401,
                         f"{endpoint} بدون auth باید ۴۰۱ بدهد، داد {resp.status_code}")

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.admin.id)
            sess["_fresh"] = True

    # ── ۱) POST /api/import/analyze ─────────────────────────────────
    # E.6.1: analyze فقط *فایل مبدأ* را می‌خواند و آن فایل همیشه SQLite است
    # (core.db.connect_to بک‌اند را قفل می‌کند) ⇒ این تست زیر PG هم اجرا می‌شود؛
    # نوشتن (confirm) روی PG بازنشسته است و پوششش در test_import_db_guard.py هست.
    def test_import_analyze_route(self):
        anon = self.client.post("/api/import/analyze")
        self._assert_anonymous_denied(anon, "POST /api/import/analyze")

        self._login_admin()
        resp = self.client.post(
            "/api/import/analyze",
            data={"file": (io.BytesIO(_uploaded_db_bytes()), "upload.db")},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()

        # SQL واقعی روی فایل DB آپلودی اجرا شده است
        self.assertEqual(body["status"], "ok")
        self.assertIn("logs", body["summary"]["importable_tables"])
        self.assertEqual(body["summary"]["logs_count"], 1)
        self.assertIn(SEED_IP, [p["ip"] for p in body["summary"]["all_printers"]])
        # اتصال به مسیر درست (فایل موقت همین دایرکتوری) انجام شده است
        self.assertTrue([f for f in os.listdir(".") if f.startswith("temp_import_")])

    # ── ۲) GET /api/export/logs ─────────────────────────────────────
    def test_export_logs_route(self):
        anon = self.client.get("/api/export/logs")
        self._assert_anonymous_denied(anon, "GET /api/export/logs")

        self._login_admin()
        resp = self.client.get("/api/export/logs?format=csv")
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        # ردیف seed‌شده در خروجی CSV هست ⇒ SELECT واقعی روی logs اجرا شده
        self.assertIn(SEED_IP, text)
        self.assertIn("PRINT", text)

    # ── ۳) GET /api/stats/daily ─────────────────────────────────────
    # «آمار روزانه» روی DB خالیِ هر chdir تعریف شده؛ روی PG همهٔ تست‌ها یک
    # اسکیمای جلسه‌ای را می‌بینند ⇒ sqlite_only (docs/phase-e-plan.md §۵.۳).
    @pytest.mark.sqlite_only
    def test_stats_daily_route(self):
        anon = self.client.get("/api/stats/daily")
        self._assert_anonymous_denied(anon, "GET /api/stats/daily")

        self._login_admin()
        resp = self.client.get(f"/api/stats/daily?ip={SEED_IP}&days=30")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["printer_ip"], SEED_IP)
        self.assertEqual(body["days"], 30)
        self.assertEqual(len(body["dates"]), 31)       # days + 1 روز
        self.assertEqual(len(body["totals"]), 31)
        self.assertEqual(body["dates"], sorted(body["dates"]))

        # SEED_DAY دینامیک («امروز») است ⇒ همیشه داخل پنجره‌ی ۳۰ روزه ⇒ ادعای
        # قطعی: ۳ صفحه‌ی seed‌شده باید در سبد همان روز بیفتد (بدون راه فرار).
        self.assertIn(SEED_DAY, body["dates"],
                      "روز seed باید داخل پنجرهٔ آماری باشد (SEED_DAY دینامیک است)")
        self.assertEqual(body["totals"][body["dates"].index(SEED_DAY)], SEED_PAGES,
                         "تجمیع روزانهٔ SQL باید ۳ صفحهٔ seed را در روز جاری ببیند")

    # ── ۳ب) فیلتر بازه‌ی زمانی روی /api/logs/all (E.3.7) ───────────
    def test_logs_all_accepts_local_date_filter(self):
        """فیلتر تاریخِ محلی باید هم‌فرمت داده‌ی UTC/Z شود و ردیف امروز را بگیرد.

        قاعده‌ی ``core.time_utils.to_filter_bound``: ``2026-10-04`` = نیمه‌شبِ
        **محلی** همان روز و ``2026-10-05`` = نیمه‌شبِ محلیِ روز بعد ⇒ بازه‌ی
        ``[امروز، فردا)`` دقیقاً «همهٔ امروزِ محلی» است.
        """
        self._login_admin()
        tomorrow = (datetime.now(SEED_LOCAL_TZ).date() + timedelta(days=1)).isoformat()
        resp = self.client.get(f"/api/logs/all?ip={SEED_IP}&start={SEED_DAY}&end={tomorrow}")
        self.assertEqual(resp.status_code, 200)
        events = resp.get_json()["events"]
        self.assertEqual(len(events), 1, f"ردیف امروز باید در بازه بیفتد: {events}")
        self.assertEqual(events[0]["type"], "PRINT")
        # timestamp خروجی همان فرمت استاندارد است (نه naive محلی)
        self.assertTrue(events[0]["timestamp"].endswith("Z"))

        # بازه‌ای که روز قبل تمام می‌شود ⇒ هیچ ردیفی نباید در آن بیفتد
        resp_past = self.client.get(f"/api/logs/all?ip={SEED_IP}&end={SEED_DAY}")
        self.assertEqual(resp_past.status_code, 200)
        self.assertEqual(resp_past.get_json()["events"], [],
                         "پایانِ بازه = نیمه‌شبِ محلیِ همان روز ⇒ ردیف امروز نباید داخل بیفتد")

    # ── ۳پ) ردیف مهاجرت‌نشده سبد آمار را خراب نمی‌کند (E.3.7) ───────
    # «آمار روزانه» روی DB خالیِ هر chdir تعریف شده؛ روی PG همهٔ تست‌ها یک
    # اسکیمای جلسه‌ای را می‌بینند ⇒ sqlite_only (docs/phase-e-plan.md §۵.۳).
    @pytest.mark.sqlite_only
    def test_stats_daily_skips_unmigrated_rows(self):
        """timestamp naive (مهاجرت‌نشده) باید skip + هشدار شود، نه ۵۰۰ یا عدد جعلی."""
        naive_ts = f"{SEED_DAY}T23:59:00"          # فرمت قدیمی: naive محلی
        with sqlite3.connect(os.path.join(os.getcwd(), "logs.db")) as raw:
            raw.execute("INSERT INTO logs (printer_ip, printer_name, timestamp, type, pages, details)"
                        " VALUES (?, ?, ?, 'PRINT', ?, '{}')",
                        (SEED_IP, "Smoke Printer", naive_ts, 999))
            raw.commit()

        self._login_admin()
        with self.assertLogs("PrinterMonitor", level="WARNING") as ctx:
            resp = self.client.get(f"/api/stats/daily?ip={SEED_IP}&days=30")
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["totals"][body["dates"].index(SEED_DAY)], SEED_PAGES,
                         "ردیف مهاجرت‌نشده (۹۹۹ صفحه) نباید به سبد اضافه شود")
        self.assertTrue(any("نامعتبر" in line for line in ctx.output),
                        f"هشدار ردیف مهاجرت‌نشده لاگ نشد: {ctx.output}")

    # ── ۴) GET /api/security/events ─────────────────────────────────
    def test_security_events_route(self):
        anon = self.client.get("/api/security/events")
        self._assert_anonymous_denied(anon, "GET /api/security/events")

        self._login_admin()
        resp = self.client.get("/api/security/events?limit=10")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertGreaterEqual(body["total"], 1)
        types = [e.get("event_type") for e in body["events"]]
        self.assertIn(SecurityEvent.SUCCESSFUL_LOGIN.value, types)
        self.assertTrue(body["available_types"])


    # ── ۵) عدم نشت داده در پاسخ ۴۰۱ (تست اضافی E.0.6) ──────────────
    def test_401_does_not_leak_printer_info(self):
        """پاسخ ۴۰۱ نباید داده‌ی سمت‌سرور را لو بدهد.

        هر مسیر **دو بار** آزموده می‌شود:
          ۱) بدون پارامتر — ساده‌ترین حالت؛
          ۲) با یک IP جعلی بیرون از DB (``203.0.113.99``، محدوده‌ی TEST-NET-3)
             — اگر پاسخ هر داده‌ی *واقعی* را برگرداند (IP seed‌شده، نام پرینتر،
             نوع رویداد)، همین‌جا لو می‌رود.

        نکته‌ی مهم (کشف‌شده در همین گام): پاسخ ۴۰۱ روبه‌روی ``login_url`` یک
        پارامتر ``next`` دارد که **همان درخواستِ خودِ فراخوان** را بازتاب می‌دهد
        (ورودی کاربر، نه داده‌ی سرور) — پس در حالت پارامتردار، IP جعلی خودش
        برمی‌گردد و این *نشت نیست*. برای همین، آزمون پارامتردار روی «نبودِ داده‌ی
        سرور» و «ساختار ثابتِ پاسخ» تمرکز می‌کند، و بازتاب ورودی جداگانه در
        ``docs/tech-debt.md`` (TD-002) ثبت شده است.
        """
        decoy_ip = "203.0.113.99"          # بیرون از DB ⇒ هر دیده‌شدنی، نشت است
        endpoints = (
            ("POST /api/import/analyze", "post", "/api/import/analyze"),
            ("GET /api/export/logs", "get", "/api/export/logs"),
            ("GET /api/stats/daily", "get", "/api/stats/daily"),
            ("GET /api/security/events", "get", "/api/security/events"),
        )
        for endpoint, method, path in endpoints:
            for suffix in ("", f"?ip={decoy_ip}&limit=10"):
                anon = getattr(self.client, method)(f"{path}{suffix}")
                self._assert_anonymous_denied(anon, f"{endpoint}{suffix}")
                lowered = anon.get_data(as_text=True).lower()
                for marker in LEAK_MARKERS:
                    self.assertNotIn(marker.lower(), lowered,
                                     f"{endpoint}{suffix}: پاسخ ۴۰۱ عبارت "
                                     f"«{marker}» را نشت داد")
                # ساختار پاسخ باید ثابت و حداقلی باشد: فقط error + login_url.
                # (چیزی جز این دو کلید یعنی مسیر ناشناس شروع کرده به توضیح دادن.)
                self.assertEqual(anon.headers.get("Content-Type", "").split(";")[0],
                                 "application/json",
                                 f"{endpoint}{suffix}: پاسخ ۴۰۱ باید JSON باشد")
                payload = anon.get_json()
                self.assertEqual(set(payload) - {"error", "login_url"}, set(),
                                 f"{endpoint}{suffix}: کلید ناشناخته در پاسخ ۴۰۱")
                self.assertEqual(payload.get("error"), "unauthorized")
                self.assertTrue(str(payload.get("login_url", "")).startswith("/login?next="),
                                f"{endpoint}{suffix}: login_url نامنتظره")
                self.assertLess(len(lowered), 300,
                                f"{endpoint}{suffix}: بدنهٔ ۴۰۱ بزرگ‌تر از حد انتظار")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
