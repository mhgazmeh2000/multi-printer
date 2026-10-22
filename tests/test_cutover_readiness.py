# -*- coding: utf-8 -*-
"""
tests/test_cutover_readiness.py — آزمون‌های ابزار `tools/check_cutover_readiness.py` (فاز E.7.2).

چرا این تست‌ها لازم‌اند؟
-----------------------
خروجی این ابزار تصمیمِ «cutover مجاز است یا نه» را می‌سازد؛ پس سه چیز باید قفل باشد:

  ۱) **تجزیهٔ DSN** — همان سه قالبی که در مستندات/کد واقعی است (SQLAlchemy URL،
     conninfoِ libpq، و خالی⇒متغیرهای محیطی)، و اینکه `options=-csearch_path=…` گم نشود؛
  ۲) **حکمِ کد خروج** — ۰ آماده / ۱ مسدود / ۲ فقط هشدار، و `--strict` که زرد را قرمز می‌کند؛
  ۳) **منطق قرمز/زرد شدن** — بدون اتصال به شبکه: با stub و با دیتابیس‌های موقت.

قصدِ آزمون ۱۰ (PG زنده) هم همین است: در سوئیت PG، سنجه‌های ۴ و ۵ باید روی همان
اسکیمای ایزولهٔ `tests/conftest.py` سبز شوند (با `skipUnless`؛ در سوئیت SQLite skip
می‌شوند — قاعدهٔ فاز E).

هیچ تستی در این فایل روی `logs.db` مخزن نمی‌نویسد؛ همه‌ی نوشتن‌ها در `tmp` است.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io as _io
import json
import os
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = ROOT / "tools" / "check_cutover_readiness.py"

ENV_PG_DSN = "PG_DSN"
_PG_DSN = (os.getenv(ENV_PG_DSN, "") or "").strip()


def _tool():
    """ماژول ابزار را با importlib بارگذاری می‌کند (tools/ بسته نیست)."""
    spec = importlib.util.spec_from_file_location("cutover_readiness_tool", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_logs_db(path: Path, timestamps: tuple) -> None:
    """SQLite موقت با حداقلِ جدول لازم برای سنجه‌های ۶ و ۷ (و marker)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE logs(id INTEGER PRIMARY KEY, timestamp TEXT, type TEXT);"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
    )
    for stamp in timestamps:
        conn.execute("INSERT INTO logs(timestamp, type) VALUES(?, 'PRINT')", (stamp,))
    conn.commit()
    conn.close()


class _FakeCursor:
    """کمینه‌ترین cursor: فقط SQLهای مورد انتظار ابزار را پاسخ می‌دهد."""

    def __init__(self, rows: dict, log: list, raise_for: tuple = ()):
        self._rows, self._log, self._raise_for = rows, log, raise_for
        self._current: list = []

    def execute(self, sql, *a, **k):
        self._log.append(sql)
        key = sql.upper()
        if any(frag.upper() in key for frag in self._raise_for):
            raise RuntimeError('relation "alembic_version" does not exist')
        for name, rows in self._rows.items():
            if name.upper() in key:
                self._current = rows
                return self
        self._current = []
        return self

    def fetchone(self):
        return tuple(self._current[0]) if self._current else None

    def fetchall(self):
        return [tuple(r) for r in self._current]


class _FakeConn:
    def __init__(self, rows: dict, log: list, raise_for: tuple = ()):
        self._rows, self._log, self._raise_for = rows, log, raise_for

    def cursor(self):
        return _FakeCursor(self._rows, self._log, self._raise_for)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_connect(rows: dict, log: list, raise_for: tuple = ()):
    def _conn(cfg, timeout=5):
        return _FakeConn(rows, log, raise_for)

    return _conn


class ParseDsnTests(unittest.TestCase):
    def setUp(self):
        self.t = _tool()

    def test_sqlalchemy_url(self):
        cfg = self.t.parse_dsn("postgresql+psycopg://pguser:pgpass@dbhost:6432/pgdb")
        self.assertEqual(cfg["host"], "dbhost")
        self.assertEqual(cfg["port"], "6432")
        self.assertEqual(cfg["dbname"], "pgdb")
        self.assertEqual(cfg["user"], "pguser")
        self.assertEqual(cfg["password"], "pgpass")
        self.assertEqual(cfg["source"], "--dsn")

    def test_conninfo_form(self):
        cfg = self.t.parse_dsn(
            "host=127.0.0.1 port=5432 dbname=printguard user=printguard password=secret"
        )
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertEqual(cfg["dbname"], "printguard")
        self.assertEqual(cfg["password"], "secret")

    def test_options_are_preserved(self):
        """`options=-csearch_path=…` نباید گم شود (ایزولاسیون تست‌ها به آن تکیه می‌کند)."""
        cfg = self.t.parse_dsn("host=h dbname=d options=-csearch_path=printguard_test_abc")
        self.assertEqual(cfg["options"], "-csearch_path=printguard_test_abc")
        kwargs = self.t.connect_kwargs(cfg)
        self.assertEqual(kwargs["options"], "-csearch_path=printguard_test_abc")
        self.assertEqual(kwargs["port"], 5432)          # int، نه str
        self.assertEqual(kwargs["connect_timeout"], 5)

    def test_empty_dsn_uses_libpq_env(self):
        with mock.patch.dict(os.environ, {"PGHOST": "via-env", "PGPORT": "6543"}, clear=False):
            cfg = self.t.parse_dsn("")
        self.assertEqual(cfg["source"], "env-libpq")
        self.assertEqual(cfg["host"], "via-env")
        self.assertEqual(cfg["port"], "6543")

    def test_mask_never_leaks_password(self):
        cfg = self.t.parse_dsn("postgresql://u:s3cr3t@h/db")
        masked = self.t.mask(cfg)
        self.assertNotIn("s3cr3t", masked)
        self.assertIn("password=***", masked)


class HeadDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.t = _tool()

    def test_repo_has_single_head_0007(self):
        heads = self.t.discover_heads(ROOT / "alembic" / "versions")
        self.assertEqual(heads, ["0007"], "سرشاخهٔ مخزن باید یکی و 0007 باشد")

    def test_multiple_heads_are_all_reported(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "0001_a.py").write_text('revision = "0001"\ndown_revision = None\n', encoding="utf-8")
            (d / "0002_b.py").write_text('revision: str = "0002"\ndown_revision: str | None = "0001"\n',
                                         encoding="utf-8")
            (d / "0003_c.py").write_text('revision = "0003"\ndown_revision = "0001"\n', encoding="utf-8")
            self.assertEqual(self.t.discover_heads(d), ["0002", "0003"])
            self.assertEqual(self.t.discover_heads(d / "missing"), [])


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.t = _tool()
        self.ok = self.t.ok("a", "الف")
        self.warn = self.t.warn("b", "ب")
        self.fail = self.t.fail("c", "ج")
        self.skip = self.t.skipped("d", "د")

    def test_all_green_is_zero(self):
        self.assertEqual(self.t.exit_code([self.ok, self.skip]), self.t.EXIT_OK)

    def test_warning_is_two(self):
        self.assertEqual(self.t.exit_code([self.ok, self.warn]), self.t.EXIT_WARN)

    def test_failure_beats_warning(self):
        self.assertEqual(self.t.exit_code([self.warn, self.fail]), self.t.EXIT_BLOCKED)

    def test_strict_promotes_warning(self):
        self.assertEqual(self.t.exit_code([self.ok, self.warn], strict=True), self.t.EXIT_BLOCKED)

    def test_render_marks_legend_and_colors(self):
        text = self.t.render([self.ok, self.warn, self.fail, self.skip], color=False,
                             title="عنوان")
        for glyph in ("✔", "!", "✘", "–"):
            self.assertIn(glyph, text)
        self.assertIn("مسدود", text)
        self.assertNotIn("\033[", text)          # بدون رنگ ⇒ هیچ ANSI در متن نباشد
        self.assertIn("\033[31m", self.t.render([self.fail], color=True, title="ع"))
        with_action = self.t.render([self.t.fail("x", "ی", "جزئیات", "اقدامِ لازم")],
                                     color=False, title="ع")
        self.assertIn("اقدامِ لازم", with_action)
        self.assertIn("↳", with_action)


class IntegrityAndMarkerTests(unittest.TestCase):
    def setUp(self):
        self.t = _tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_good_db_reports_rows_and_warns_on_missing_tables(self):
        db = self.dir / "logs.db"
        _make_logs_db(db, ("2026-10-04T10:00:00Z", "2026-10-04T11:00:00Z"))
        res = self.t.check_integrity(db)
        self.assertEqual(res.status, "warn")     # فقط ۲ جدول ⇒ «کمتر از ۱۱ جدول برنامه»
        self.assertIn("integrity=ok", res["detail"])
        self.assertIn("logs=2", res["detail"])

    def test_full_app_db_is_green(self):
        """با همان ۱۱ جدولِ APP_TABLES ⇒ سنجهٔ ۶ سبز است (شمارش‌ها به مخزن گره خورده)."""
        from core.db.schema import APP_TABLES

        db = self.dir / "full.db"
        conn = sqlite3.connect(db)
        for table in APP_TABLES:
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, timestamp TEXT)')
        conn.commit()
        conn.close()
        self.assertGreaterEqual(len(APP_TABLES), 11)
        res = self.t.check_integrity(db)
        self.assertEqual(res.status, "ok", res["detail"])

    def test_garbage_file_fails(self):
        bad = self.dir / "bad.db"
        bad.write_bytes(b"this is not a sqlite file" * 30)
        res = self.t.check_integrity(bad)
        self.assertEqual(res.status, "fail")
        self.assertTrue(res["action"])

    def test_missing_file_fails_with_action(self):
        res = self.t.check_integrity(self.dir / "nope.db")
        self.assertEqual(res.status, "fail")
        self.assertIn("--db", res["action"])

    def test_marker_absent_is_ok_and_present_is_warn(self):
        db = self.dir / "logs.db"
        _make_logs_db(db, ())
        self.assertEqual(self.t.check_marker(db).status, "ok")
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO meta(key, value) VALUES('cutover_utc', '2026-10-04T00:00:00Z')")
        conn.commit()
        conn.close()
        res = self.t.check_marker(db)
        self.assertEqual(res.status, "warn")
        self.assertIn("2026-10-04T00:00:00Z", res["detail"])


class TimestampsTests(unittest.TestCase):
    """سنجهٔ ۷ با همان `analyze_old_events.py` ⇒ قرارداد Z/Asia-Tehran قفل می‌ماند."""

    def setUp(self):
        self.t = _tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_after_cutover_z_is_green(self):
        db = self.dir / "logs.db"
        _make_logs_db(db, ("2026-10-04T10:00:00Z",))
        res = self.t.check_timestamps(db)
        self.assertEqual(res.status, "ok", res["detail"])
        self.assertIn("ردیف‌های نیازمند بازبینی=0", res["detail"])

    def test_before_cutover_z_is_yellow_not_red(self):
        db = self.dir / "logs.db"
        _make_logs_db(db, ("2026-01-02T10:00:00Z", "2026-01-03T10:00:00Z"))
        res = self.t.check_timestamps(db)
        self.assertEqual(res.status, "warn", res["detail"])
        self.assertIn("F.5", res["action"])
        self.assertIn("ردیف‌های نیازمند بازبینی=2", res["detail"])

    def test_explicit_cutover_is_forwarded(self):
        db = self.dir / "logs.db"
        _make_logs_db(db, ("2026-10-01T10:00:00Z",))
        res = self.t.check_timestamps(db, cutover="2026-09-01T00:00:00Z")
        self.assertEqual(res.status, "ok", res["detail"])   # همان ردیف حالا پس از cutover است

    def test_missing_tool_is_skip_not_crash(self):
        db = self.dir / "logs.db"
        _make_logs_db(db, ())
        self.assertEqual(self.t.check_timestamps(db, analyze_tool=self.dir / "nothing.py").status,
                         "skip")


class DiskTests(unittest.TestCase):
    def setUp(self):
        self.t = _tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "logs.db"
        self.db.write_bytes(b"x" * 1000)

    def _with_free(self, free_bytes: int):
        usage = type("U", (), {"total": free_bytes * 4, "used": free_bytes * 3,
                               "free": free_bytes})()
        with mock.patch.object(self.t.shutil, "disk_usage", return_value=usage):
            return self.t.check_disk(self.db)

    def test_tiny_free_is_red(self):
        self.assertEqual(self._with_free(1000).status, "fail")          # < ۲×

    def test_middle_free_is_yellow(self):
        self.assertEqual(self._with_free(3000).status, "warn")           # بین ۲× و ۴×

    def test_plenty_is_green(self):
        res = self._with_free(10 ** 9)
        self.assertEqual(res.status, "ok")
        self.assertIn("logs.db=1,000 B", res["detail"])


class PortAndServiceTests(unittest.TestCase):
    def setUp(self):
        self.t = _tool()

    def _listener(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        return srv.getsockname()[1]

    def test_open_port_green_closed_red(self):
        port = self._listener()
        self.assertEqual(self.t.check_port({"host": "127.0.0.1", "port": str(port)}).status, "ok")
        dead = self._listener_free_port()
        self.assertEqual(self.t.check_port({"host": "127.0.0.1", "port": str(dead)}, timeout=1).status,
                         "fail")

    def _listener_free_port(self):
        """پورتی که همین اول باز شد و بسته شد ⇒ شنونده‌ای ندارد."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        return port

    def test_service_falls_back_to_socket_without_pg_isready(self):
        port = self._listener()
        cfg = {"host": "127.0.0.1", "port": str(port), "dbname": "", "user": ""}
        with mock.patch.object(self.t.shutil, "which", return_value=None):
            res = self.t.check_service(cfg)
        self.assertEqual(res.status, "ok")
        self.assertIn("pg_isready", res["detail"])

    def test_service_uses_pg_isready_when_present(self):
        cfg = {"host": "h", "port": "1", "dbname": "d", "user": "u"}
        log = []

        def fake_run(cmd, **k):
            log.append(cmd)
            return type("P", (), {"returncode": 0, "stdout": "h:1 - accepting connections",
                                  "stderr": ""})()

        with mock.patch.object(self.t.shutil, "which", return_value="/usr/bin/pg_isready"), \
                mock.patch.object(self.t.subprocess, "run", side_effect=fake_run):
            res = self.t.check_service(cfg)
        self.assertEqual(res.status, "ok")
        self.assertEqual(log[0][:3], ["/usr/bin/pg_isready", "-h", "h"])
        self.assertIn("accepting connections", res["detail"])


class DsnAndHeadWithStubTests(unittest.TestCase):
    def setUp(self):
        self.t = _tool()
        self.cfg = self.t.parse_dsn("host=h dbname=d user=u")

    def test_dsn_green_and_logs_the_probe(self):
        log = []
        rows = {"SELECT 1": [[1]],
                "CURRENT_DATABASE": [("d", "PostgreSQL 16.15 on x86_64")]}
        res = self.t.check_dsn(self.cfg, connect=_fake_connect(rows, log))
        self.assertEqual(res.status, "ok", res["detail"])
        self.assertIn("SELECT 1", log[0])
        self.assertIn("PostgreSQL 16.15", res["detail"])

    def test_dsn_error_is_red_with_action(self):
        def boom(cfg, timeout=5):
            raise OSError('connection to server at "h" failed')

        res = self.t.check_dsn(self.cfg, connect=boom)
        self.assertEqual(res.status, "fail")
        self.assertIn("psql", res["action"])
        self.assertIn("connection to server", res["detail"])

    def test_head_matches_repo_head(self):
        log = []
        rows = {"CURRENT_SCHEMA": [("public", '"$user", public')],
                "ALEMBIC_VERSION": [("0007",)]}
        res = self.t.check_alembic_head(self.cfg, connect=_fake_connect(rows, log),
                                        versions_dir=ROOT / "alembic" / "versions")
        self.assertEqual(res.status, "ok", res["detail"])
        self.assertIn("0007", res["detail"])
        self.assertIn('search_path="$user", public', res["detail"])
        self.assertTrue(any("FROM alembic_version" in q for q in log))

    def test_head_mismatch_is_red_and_names_expected(self):
        rows = {"CURRENT_SCHEMA": [("public", "public")], "ALEMBIC_VERSION": [("0003",)]}
        res = self.t.check_alembic_head(self.cfg, connect=_fake_connect(rows, []),
                                        versions_dir=ROOT / "alembic" / "versions")
        self.assertEqual(res.status, "fail")
        self.assertIn("انتظار مخزن: 0007", res["detail"])
        self.assertIn("upgrade head", res["action"])

    def test_head_empty_table_is_red(self):
        rows = {"CURRENT_SCHEMA": [("public", "public")], "ALEMBIC_VERSION": []}
        res = self.t.check_alembic_head(self.cfg, connect=_fake_connect(rows, []),
                                        versions_dir=ROOT / "alembic" / "versions")
        self.assertEqual(res.status, "fail")
        self.assertIn("هیچ سطری ندارد", res["detail"])

    def test_head_falls_back_to_public_schema(self):
        """اگر search_path جدول را نبیند، `public.alembic_version` را امتحان می‌کند (TD-011)."""
        log = []
        rows = {"CURRENT_SCHEMA": [("x", "x")], "PUBLIC.ALEMBIC_VERSION": [("0007",)]}
        connect = _fake_connect(rows, log, raise_for=("FROM alembic_version",))
        res = self.t.check_alembic_head(self.cfg, connect=connect,
                                        versions_dir=ROOT / "alembic" / "versions")
        self.assertEqual(res.status, "ok", res["detail"])
        self.assertTrue(any("public.alembic_version" in q for q in log))
        self.assertIn("public (fallback)", res["detail"])

    def test_head_without_versions_dir_is_red(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.t.check_alembic_head(self.cfg, connect=_fake_connect({}, []),
                                            versions_dir=Path(td))
        self.assertEqual(res.status, "fail")
        self.assertIn("سرشاخه", res["detail"])


class CliTests(unittest.TestCase):
    """رفتار خط فرمان: کد خروج، skipها، و نوشته‌نشدن هیچ‌چیز جز `--json`."""

    def setUp(self):
        self.t = _tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "logs.db"
        _make_logs_db(self.db, ("2026-10-04T10:00:00Z",))

    def _run(self, *argv):
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.t.main(list(argv))
        return code, buf.getvalue()

    def test_self_test_is_zero(self):
        code, out = self._run("--self-test")
        self.assertEqual(code, 0)
        self.assertIn("self-test", out)

    def test_skip_pg_marks_four_skips_and_writes_nothing(self):
        before = sorted(p.name for p in self.dir.iterdir())
        code, out = self._run("--db", str(self.db), "--skip-pg", "--color", "never")
        self.assertEqual(out.count("[–]"), 4)
        self.assertIn("چک‌لیست آمادگی cutover", out)
        self.assertIn(code, (self.t.EXIT_OK, self.t.EXIT_WARN))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

    def test_json_report_masks_password_and_has_ten_rows(self):
        out_file = self.dir / "report.json"
        with mock.patch.dict(os.environ, {"PG_DSN": "host=nohost dbname=d user=u password=pw"}):
            code, _ = self._run("--db", str(self.db), "--skip-pg", "--json", str(out_file))
        text = out_file.read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertEqual(code, data["exit_code"])
        # دقت: نام دایرکتوری tmp می‌تواند تصادفاً «pw» داشته باشد ⇒ رمز را دقیق می‌سنجیم
        self.assertNotIn("password=pw", text)
        self.assertNotIn("pw\n", text)
        self.assertIn("password=***", text)
        self.assertEqual(len(data["checks"]), 10)         # ۹ سنجه + ۱ ردیف کمکی marker
        self.assertEqual(data["expected_heads"], ["0007"])
        self.assertEqual({r["key"] for r in data["checks"]},
                         {"python", "psycopg", "service", "dsn", "head", "port",
                          "integrity", "timestamps", "disk", "marker"})

    def test_broken_dsn_is_blocked_exit_one(self):
        """بدون `--skip-pg` و با DSN خراب ⇒ قرمز ⇒ ۱ (cutover مجاز نیست)."""
        with mock.patch.dict(os.environ,
                             {"PG_DSN": "host=127.0.0.1 port=1 dbname=none user=none"},
                             clear=False):
            code, out = self._run("--db", str(self.db), "--color", "never")
        self.assertEqual(code, self.t.EXIT_BLOCKED)
        self.assertIn("[✘]", out)
        self.assertIn("cutover مجاز نیست", out)

    def test_strict_turns_yellow_into_one(self):
        db = self.dir / "pre.db"
        _make_logs_db(db, ("2026-01-05T10:00:00Z",))          # Z پیش از cutover ⇒ زرد
        soft, out_soft = self._run("--db", str(db), "--skip-pg", "--color", "never")
        strict, out = self._run("--db", str(db), "--skip-pg", "--color", "never", "--strict")
        self.assertEqual(soft, self.t.EXIT_WARN)
        self.assertEqual(strict, self.t.EXIT_BLOCKED)
        self.assertIn("آماده با هشدار", out_soft)      # بدون strict: زرد، نه قرمز
        self.assertIn("مسدود", out)                   # با strict: خطِ نتیجه هم باید قرمز بگوید
        self.assertIn("--strict", out)

    def test_quiet_prints_single_verdict_line(self):
        code, out = self._run("--db", str(self.db), "--skip-pg", "--quiet")
        self.assertIn("readiness:", out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.strip()]), 2)  # خط نتیجه + پیام پایانی


@unittest.skipUnless(_PG_DSN, f"{ENV_PG_DSN} ست نشده ⇒ سنجه‌های زندهٔ PG اجرا نمی‌شوند")
class LivePgTests(unittest.TestCase):
    """PG واقعی: سنجه‌های ۴ و ۵ سبز (در سوئیت PG = همان اسکیمای ایزولهٔ conftest)."""

    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("psycopg") is None:
            raise unittest.SkipTest("psycopg نصب نیست ⇒ سنجه‌های زندهٔ PG اجرا نمی‌شوند")
        cls.t = _tool()
        cls.cfg = cls.t.parse_dsn(_PG_DSN)

    def test_live_dsn_and_head(self):
        dsn_res = self.t.check_dsn(self.cfg)
        if dsn_res.status != "ok":
            self.skipTest(f"PG در دسترس نیست: {dsn_res['detail'][:120]}")
        self.assertEqual(dsn_res.status, "ok")
        head = self.t.check_alembic_head(self.cfg, versions_dir=ROOT / "alembic" / "versions")
        self.assertEqual(head.status, "ok", head["detail"])
        self.assertIn("0007", head["detail"])

    def test_live_port_check(self):
        port = int(self.cfg.get("port") or 5432)
        host = self.cfg.get("host") or "localhost"
        try:
            with socket.create_connection((host, port), timeout=3):
                pass
        except OSError as exc:
            self.skipTest(f"پورت PG باز نیست: {exc}")
        self.assertEqual(self.t.check_port(self.cfg).status, "ok")


if __name__ == "__main__":
    unittest.main()
