# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_smoke_routes.py
# @brief:        Smoke tests for routes that use raw SQL (phase D).
# @details:      Responsibilities: Check that the routes respond correctly against seeded data.
#                Main API: SmokeRoutesTests
#                Side effects: database access, file I/O
# @dependencies: internal: core.database, core.security_audit, core.time_utils, models, web |
#                external: pytest
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Smoke test routes with raw SQL (Phase D — Task D.3).

Why this file?
    The following four paths in Phase E should also work on PostgreSQL. These tests are a tour
    They are quick safety so that if one day the connection/SQL of these routes breaks, it will be exposed right there
    (not in deep domain tests).

Routes:
    POST /api/import/analyze → Connect to upload temporary DB file (non-default path)
    GET /api/export/logs → SELECT ON logs
    GET /api/stats/daily → SELECT/GROUP BY on logs
    GET /api/security/events → SELECT ON security_events

Each test has three assertions (E.0.6 made it difficult):
    1) No entry ⇒ **exactly 401** (previously it was "200 or 401"; if one day a route
       accidentally opened, the old test remains green and no one understands);
    2) By entering admin ⇒ 200 **and the actual SQL result** (not just the status code). value
       The main point of this tour is that the queries are really executed; Because the anonymous response is 401
       and does not see any SQL;
    3) The 401 body does not leak any real data (IP, printer name, event type).

Note E.0.6: ``SEED_DAY'' is dynamic (current day); Fixed before ``2026-10-01''
and after 30 days, the claim of "3 pages in that day" would become invalid.

Note E.3.7: From phase E.3, the stored data is UTC/Z (fixed length 27) and the daily basket in
``/api/stats/daily'' is **local** day ⇒ ``seed_ts()'' creates string from local time
so that mean "day" and "moment" are the same.

Execution:
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
# The seed day is dynamic (E.0.6): always "today" ⇒ also within the 30-day statistical window
# It remains, the test does not become ineffective after the passage of time. A single value for all three
# consumer (add_event, DB upload, daily aggregation claim) is used.
#
# Phase E.3: The standard DB data is UTC/Z and the basket of daily statistics is "local day."
# (Asia/Tehran)" ⇒ The seed day and its time string must be made from the same origin.
# Otherwise, test between 20:30 and 24:00 in Tehran (UTC day is different from local day).
# It was unstable.
SEED_LOCAL_TZ = local_tz()
SEED_DAY = datetime.now(SEED_LOCAL_TZ).date().isoformat()


def seed_ts(hour: int = 10, minute: int = 5) -> str:
    """Standard timestamp (UTC/Z) for the local time given in "local today"."""
    day = datetime.now(SEED_LOCAL_TZ).date()
    return to_utc_z(datetime.combine(day, time(hour, minute), tzinfo=SEED_LOCAL_TZ))
SEED_PAGES = 3
# Phrases that should never appear in an anonymous response (leakage test)
LEAK_MARKERS = (SEED_IP, "Smoke Printer", "PRINT", "REFILL")


def _uploaded_db_bytes() -> bytes:
    """
    An actual ``.db'' file (created in temporary file, not memory) with the logs table.

    Why file: path ``/api/import/analyze'' with ``sqlite3.connect(path)'' on
    The saved file works; This route cannot be tested with a memory DB.
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

    """Tests for smoke routes."""
    def setUp(self):
        # DB_PATH is relative ⇒ with chdir, each test has its own temporary DB.
        """Prepare the fixtures that each test in this class uses."""
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
        """Release the resources that each test in this class created."""
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _assert_anonymous_denied(self, resp, endpoint: str):
        """No login ⇒ **exactly 401** (not 200 and not any other code).

        If the answer is 200, it will first fail with an explicit message to find the cause;
        Then the exact equality with 401 is checked.
        """
        if resp.status_code == 200:
            self.fail(f"نشت دسترسی: endpoint {endpoint} بدون auth پاسخ ۲۰۰ داد")
        self.assertEqual(resp.status_code, 401,
                         f"{endpoint} بدون auth باید ۴۰۱ بدهد، داد {resp.status_code}")

    def _login_admin(self):
        """Helper: login admin."""
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.admin.id)
            sess["_fresh"] = True

    # 1) POST /api/import/analyze
    # E.6.1: analyze only reads the *source file* and that file is always SQLite
    # (core.db.connect_to locks the backend) ⇒ This test is also executed under PG;
    # Write (confirm) is deprecated on PG and is covered in test_import_db_guard.py.
    def test_import_analyze_route(self):
        """Verifies import analyze route."""
        anon = self.client.post("/api/import/analyze")
        self._assert_anonymous_denied(anon, "POST /api/import/analyze")

        self._login_admin()
        resp = self.client.post(
            "/api/import/analyze",
            data={"file": (io.BytesIO(_uploaded_db_bytes()), "upload.db")},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()

        # The actual SQL is executed on the uploaded DB file
        self.assertEqual(body["status"], "ok")
        self.assertIn("logs", body["summary"]["importable_tables"])
        self.assertEqual(body["summary"]["logs_count"], 1)
        self.assertIn(SEED_IP, [p["ip"] for p in body["summary"]["all_printers"]])
        # The connection to the correct path (the temporary file of this directory) has been made
        self.assertTrue([f for f in os.listdir(".") if f.startswith("temp_import_")])

    # 2) GET /api/export/logs
    def test_export_logs_route(self):
        """Verifies export logs route."""
        anon = self.client.get("/api/export/logs")
        self._assert_anonymous_denied(anon, "GET /api/export/logs")

        self._login_admin()
        resp = self.client.get("/api/export/logs?format=csv")
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        # The seeded row is in the CSV output ⇒ the actual SELECT is executed on the logs
        self.assertIn(SEED_IP, text)
        self.assertIn("PRINT", text)

    # 3) GET /api/stats/daily
    # "daily statistics" defined on the empty DB of each chdir; On PG, all tests are one
    # See the session schema ⇒ sqlite_only (docs/phase-e-plan.md §5.3).
    @pytest.mark.sqlite_only
    def test_stats_daily_route(self):
        """Verifies stats daily route."""
        anon = self.client.get("/api/stats/daily")
        self._assert_anonymous_denied(anon, "GET /api/stats/daily")

        self._login_admin()
        resp = self.client.get(f"/api/stats/daily?ip={SEED_IP}&days=30")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["printer_ip"], SEED_IP)
        self.assertEqual(body["days"], 30)
        self.assertEqual(len(body["dates"]), 31)       # days + 1 day
        self.assertEqual(len(body["totals"]), 31)
        self.assertEqual(body["dates"], sorted(body["dates"]))

        # SEED_DAY is dynamic ("today") ⇒ always within a 30-day window ⇒ claim
        # Definitive: 3 seeded pages must fall into the basket on the same day (no escape).
        self.assertIn(SEED_DAY, body["dates"],
                      "روز seed باید داخل پنجرهٔ آماری باشد (SEED_DAY دینامیک است)")
        self.assertEqual(body["totals"][body["dates"].index(SEED_DAY)], SEED_PAGES,
                         "تجمیع روزانهٔ SQL باید ۳ صفحهٔ seed را در روز جاری ببیند")

    # ── 3b) time interval filter on /api/logs/all (E.3.7) ───────────
    def test_logs_all_accepts_local_date_filter(self):
        """The local date filter should also be UTC/Z data format and take today's row.

        ``core.time_utils.to_filter_bound'' rule: ``2026-10-04'' = midnight
        **local** the same day and ``2026-10-05'' = local midnight of the next day ⇒ interval
        ``[today, tomorrow)'' is exactly ``all local today''.
        """
        self._login_admin()
        tomorrow = (datetime.now(SEED_LOCAL_TZ).date() + timedelta(days=1)).isoformat()
        resp = self.client.get(f"/api/logs/all?ip={SEED_IP}&start={SEED_DAY}&end={tomorrow}")
        self.assertEqual(resp.status_code, 200)
        events = resp.get_json()["events"]
        self.assertEqual(len(events), 1, f"ردیف امروز باید در بازه بیفتد: {events}")
        self.assertEqual(events[0]["type"], "PRINT")
        # output timestamp is the standard format (not local naive)
        self.assertTrue(events[0]["timestamp"].endswith("Z"))

        # The range that ends the previous day ⇒ no row should fall in it
        resp_past = self.client.get(f"/api/logs/all?ip={SEED_IP}&end={SEED_DAY}")
        self.assertEqual(resp_past.status_code, 200)
        self.assertEqual(resp_past.get_json()["events"], [],
                         "پایانِ بازه = نیمه‌شبِ محلیِ همان روز ⇒ ردیف امروز نباید داخل بیفتد")

    # ── 3p) The unmigrated row does not destroy the statistics basket (E.3.7) ───────
    # "daily statistics" defined on the empty DB of each chdir; On PG, all tests are one
    # See the session schema ⇒ sqlite_only (docs/phase-e-plan.md §5.3).
    @pytest.mark.sqlite_only
    def test_stats_daily_skips_unmigrated_rows(self):
        """Naive timestamp should be skip + warning, not 500 or fake number."""
        naive_ts = f"{SEED_DAY}T23:59:00"          # Old format: local naive
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

    # ── 4) GET /api/security/events ───────────────────────────────────
    def test_security_events_route(self):
        """Verifies security events route."""
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


    # ── 5) No data leakage in 401 response (additional test E.0.6) ──────────────
    def test_401_does_not_leak_printer_info(self):
        """A 401 response should not expose server side data.

        Each path is tested **twice**:
          1) without parameters — the simplest case;
          2) with a fake IP outside the DB (``203.0.113.99'', TEST-NET-3 range)
             — if the response returns any *real* data (seeded IP, printer name,
             event type), is exposed here.

        Important point (discovered in this step): 401 response to ``login_url'' is one
        It has a ``next'' parameter that echoes **the same request as the call itself**
        (user input, not server data) — so in parameterized mode, its own fake IP
        It comes back and it's *not a leak*. Therefore, the parameterized test on "absence of data".
        server" and "fixed response structure" focus, and separate input reflection in
        ``docs/tech-debt.md'' (TD-002) is registered.
        """
        decoy_ip = "203.0.113.99"          # Outside the DB ⇒ anything visible is a leak
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
                # The response structure should be consistent and minimal: just error + login_url.
                # (Anything other than these two keys means the unknown path has started to explain.)
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
