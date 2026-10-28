# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_cutover_readiness.py
# @brief:        Tests for the cutover readiness checker tool (phase E.7.2).
# @details:      Responsibilities: Check DSN parsing, head discovery, verdict logic and integrity
#                markers.
#                Main API: _FakeCursor, _FakeConn, ParseDsnTests, HeadDiscoveryTests,
#                VerdictTests, IntegrityAndMarkerTests
#                Side effects: database access, network I/O, file I/O
# @dependencies: internal: core.db.schema
# @notes:        Can be run directly as a script. PostgreSQL-live tests skip automatically when no
#                server is reachable.
# =============================================================================

"""
tests/test_cutover_readiness.py — Tests for the `tools/check_cutover_readiness.py' tool (phase E.7.2).

Why are these tests necessary?
-----------------------
The output of this tool makes the decision "cutover is allowed or not"; So three things must be locked:

  1) **DSN Parse** — the same three formats as in the actual documentation/code (SQLAlchemy URL,
     libpq's conninfo, and empty⇒environment variables), and that `options=-csearch_path=…` is not missing;
  2) **exit code order** — 0 ready / 1 block / 2 warning only, and `--strict' which turns yellow to red;
  3) **Red/yellowing logic** — without network connection: with stub and with temporary databases.

The purpose of test 10 (live PG) is the same: in the PG suite, measures 4 and 5 should be on the same
Green isolated `tests/conftest.py' schema (with `skipUnless'; in SQLite suite skip
are - the rule of phase E).

No test in this file writes to the repository's `logs.db'; All writes are in `tmp`.
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
    """Loads the tools module with importlib (tools/ is not closed)."""
    spec = importlib.util.spec_from_file_location("cutover_readiness_tool", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_logs_db(path: Path, timestamps: tuple) -> None:
    """Temporary SQLite with the minimum required table for metrics 6 and 7 (and marker)."""
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
    """Minimal cursor: only answers the SQL expected by the tool."""

    def __init__(self, rows: dict, log: list, raise_for: tuple = ()):
        """Initialize the instance."""
        self._rows, self._log, self._raise_for = rows, log, raise_for
        self._current: list = []

    def execute(self, sql, *a, **k):
        """Helper: execute."""
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
        """Helper: fetchone."""
        return tuple(self._current[0]) if self._current else None

    def fetchall(self):
        """Helper: fetchall."""
        return [tuple(r) for r in self._current]


class _FakeConn:
    """Helper class: fake conn."""
    def __init__(self, rows: dict, log: list, raise_for: tuple = ()):
        """Initialize the instance."""
        self._rows, self._log, self._raise_for = rows, log, raise_for

    def cursor(self):
        """Helper: cursor."""
        return _FakeCursor(self._rows, self._log, self._raise_for)

    def __enter__(self):
        """Enter the context and return the object."""
        return self

    def __exit__(self, *exc):
        """Leave the context and release what was acquired."""
        return False


def _fake_connect(rows: dict, log: list, raise_for: tuple = ()):
    """Helper: fake connect."""
    def _conn(cfg, timeout=5):
        """Helper: conn."""
        return _FakeConn(rows, log, raise_for)

    return _conn


class ParseDsnTests(unittest.TestCase):
    """Tests for parse dsn."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.t = _tool()

    def test_sqlalchemy_url(self):
        """Verifies sqlalchemy url."""
        cfg = self.t.parse_dsn("postgresql+psycopg://pguser:pgpass@dbhost:6432/pgdb")
        self.assertEqual(cfg["host"], "dbhost")
        self.assertEqual(cfg["port"], "6432")
        self.assertEqual(cfg["dbname"], "pgdb")
        self.assertEqual(cfg["user"], "pguser")
        self.assertEqual(cfg["password"], "pgpass")
        self.assertEqual(cfg["source"], "--dsn")

    def test_conninfo_form(self):
        """Verifies conninfo form."""
        cfg = self.t.parse_dsn(
            "host=127.0.0.1 port=5432 dbname=printguard user=printguard password=secret"
        )
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertEqual(cfg["dbname"], "printguard")
        self.assertEqual(cfg["password"], "secret")

    def test_options_are_preserved(self):
        """`options=-csearch_path=…` should not be missing (isolation tests rely on it)."""
        cfg = self.t.parse_dsn("host=h dbname=d options=-csearch_path=printguard_test_abc")
        self.assertEqual(cfg["options"], "-csearch_path=printguard_test_abc")
        kwargs = self.t.connect_kwargs(cfg)
        self.assertEqual(kwargs["options"], "-csearch_path=printguard_test_abc")
        self.assertEqual(kwargs["port"], 5432)          # int, not str
        self.assertEqual(kwargs["connect_timeout"], 5)

    def test_empty_dsn_uses_libpq_env(self):
        """Verifies empty dsn uses libpq env."""
        with mock.patch.dict(os.environ, {"PGHOST": "via-env", "PGPORT": "6543"}, clear=False):
            cfg = self.t.parse_dsn("")
        self.assertEqual(cfg["source"], "env-libpq")
        self.assertEqual(cfg["host"], "via-env")
        self.assertEqual(cfg["port"], "6543")

    def test_mask_never_leaks_password(self):
        """Verifies mask never leaks password."""
        cfg = self.t.parse_dsn("postgresql://u:s3cr3t@h/db")
        masked = self.t.mask(cfg)
        self.assertNotIn("s3cr3t", masked)
        self.assertIn("password=***", masked)


class HeadDiscoveryTests(unittest.TestCase):
    """Tests for head discovery."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.t = _tool()

    def test_repo_has_single_head_0008(self):
        """Verifies repo has single head 0008."""
        heads = self.t.discover_heads(ROOT / "alembic" / "versions")
        self.assertEqual(heads, ["0008"], "سرشاخهٔ مخزن باید یکی و 0008 باشد")

    def test_multiple_heads_are_all_reported(self):
        """Verifies multiple heads are all reported."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "0001_a.py").write_text('revision = "0001"\ndown_revision = None\n', encoding="utf-8")
            (d / "0002_b.py").write_text('revision: str = "0002"\ndown_revision: str | None = "0001"\n',
                                         encoding="utf-8")
            (d / "0003_c.py").write_text('revision = "0003"\ndown_revision = "0001"\n', encoding="utf-8")
            self.assertEqual(self.t.discover_heads(d), ["0002", "0003"])
            self.assertEqual(self.t.discover_heads(d / "missing"), [])


class VerdictTests(unittest.TestCase):
    """Tests for verdict."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.t = _tool()
        self.ok = self.t.ok("a", "الف")
        self.warn = self.t.warn("b", "ب")
        self.fail = self.t.fail("c", "ج")
        self.skip = self.t.skipped("d", "د")

    def test_all_green_is_zero(self):
        """Verifies all green is zero."""
        self.assertEqual(self.t.exit_code([self.ok, self.skip]), self.t.EXIT_OK)

    def test_warning_is_two(self):
        """Verifies warning is two."""
        self.assertEqual(self.t.exit_code([self.ok, self.warn]), self.t.EXIT_WARN)

    def test_failure_beats_warning(self):
        """Verifies failure beats warning."""
        self.assertEqual(self.t.exit_code([self.warn, self.fail]), self.t.EXIT_BLOCKED)

    def test_strict_promotes_warning(self):
        """Verifies strict promotes warning."""
        self.assertEqual(self.t.exit_code([self.ok, self.warn], strict=True), self.t.EXIT_BLOCKED)

    def test_render_marks_legend_and_colors(self):
        """Verifies render marks legend and colors."""
        text = self.t.render([self.ok, self.warn, self.fail, self.skip], color=False,
                             title="عنوان")
        for glyph in ("✔", "!", "✘", "–"):
            self.assertIn(glyph, text)
        self.assertIn("مسدود", text)
        self.assertNotIn("\033[", text)          # No color ⇒ no ANSI in the text
        self.assertIn("\033[31m", self.t.render([self.fail], color=True, title="ع"))
        with_action = self.t.render([self.t.fail("x", "ی", "جزئیات", "اقدامِ لازم")],
                                     color=False, title="ع")
        self.assertIn("اقدامِ لازم", with_action)
        self.assertIn("↳", with_action)


class IntegrityAndMarkerTests(unittest.TestCase):
    """Tests for integrity and marker."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.t = _tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_good_db_reports_rows_and_warns_on_missing_tables(self):
        """Verifies good db reports rows and warns on missing tables."""
        db = self.dir / "logs.db"
        _make_logs_db(db, ("2026-10-04T10:00:00Z", "2026-10-04T11:00:00Z"))
        res = self.t.check_integrity(db)
        self.assertEqual(res.status, "warn")     # Only 2 tables ⇒ "Less than 11 program tables"
        self.assertIn("integrity=ok", res["detail"])
        self.assertIn("logs=2", res["detail"])

    def test_full_app_db_is_green(self):
        """With the same 11 tables as APP_TABLES ⇒ gauge 6 is green (counts are tied to the repository)."""
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
        """Verifies garbage file fails."""
        bad = self.dir / "bad.db"
        bad.write_bytes(b"this is not a sqlite file" * 30)
        res = self.t.check_integrity(bad)
        self.assertEqual(res.status, "fail")
        self.assertTrue(res["action"])

    def test_missing_file_fails_with_action(self):
        """Verifies missing file fails with action."""
        res = self.t.check_integrity(self.dir / "nope.db")
        self.assertEqual(res.status, "fail")
        self.assertIn("--db", res["action"])

    def test_marker_absent_is_ok_and_present_is_warn(self):
        """Verifies marker absent is ok and present is warn."""
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
    """Criterion 7 remains locked with the same `analyze_old_events.py` ⇒ Z/Asia-Tehran contract."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.t = _tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_after_cutover_z_is_green(self):
        """Verifies after cutover z is green."""
        db = self.dir / "logs.db"
        _make_logs_db(db, ("2026-10-04T10:00:00Z",))
        res = self.t.check_timestamps(db)
        self.assertEqual(res.status, "ok", res["detail"])
        self.assertIn("ردیف‌های نیازمند بازبینی=0", res["detail"])

    def test_before_cutover_z_is_yellow_not_red(self):
        """Verifies before cutover z is yellow not red."""
        db = self.dir / "logs.db"
        _make_logs_db(db, ("2026-01-02T10:00:00Z", "2026-01-03T10:00:00Z"))
        res = self.t.check_timestamps(db)
        self.assertEqual(res.status, "warn", res["detail"])
        self.assertIn("F.5", res["action"])
        self.assertIn("ردیف‌های نیازمند بازبینی=2", res["detail"])

    def test_explicit_cutover_is_forwarded(self):
        """Verifies explicit cutover is forwarded."""
        db = self.dir / "logs.db"
        _make_logs_db(db, ("2026-10-01T10:00:00Z",))
        res = self.t.check_timestamps(db, cutover="2026-09-01T00:00:00Z")
        self.assertEqual(res.status, "ok", res["detail"])   # The same row is now after cutover

    def test_missing_tool_is_skip_not_crash(self):
        """Verifies missing tool is skip not crash."""
        db = self.dir / "logs.db"
        _make_logs_db(db, ())
        self.assertEqual(self.t.check_timestamps(db, analyze_tool=self.dir / "nothing.py").status,
                         "skip")


class DiskTests(unittest.TestCase):
    """Tests for disk."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.t = _tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "logs.db"
        self.db.write_bytes(b"x" * 1000)

    def _with_free(self, free_bytes: int):
        """Helper: with free."""
        usage = type("U", (), {"total": free_bytes * 4, "used": free_bytes * 3,
                               "free": free_bytes})()
        with mock.patch.object(self.t.shutil, "disk_usage", return_value=usage):
            return self.t.check_disk(self.db)

    def test_tiny_free_is_red(self):
        """Verifies tiny free is red."""
        self.assertEqual(self._with_free(1000).status, "fail")          # < 2x

    def test_middle_free_is_yellow(self):
        """Verifies middle free is yellow."""
        self.assertEqual(self._with_free(3000).status, "warn")           # Between 2x and 4x

    def test_plenty_is_green(self):
        """Verifies plenty is green."""
        res = self._with_free(10 ** 9)
        self.assertEqual(res.status, "ok")
        self.assertIn("logs.db=1,000 B", res["detail"])


class PortAndServiceTests(unittest.TestCase):
    """Tests for port and service."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.t = _tool()

    def _listener(self):
        """Helper: listener."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        return srv.getsockname()[1]

    def test_open_port_green_closed_red(self):
        """Verifies open port green closed red."""
        port = self._listener()
        self.assertEqual(self.t.check_port({"host": "127.0.0.1", "port": str(port)}).status, "ok")
        dead = self._listener_free_port()
        self.assertEqual(self.t.check_port({"host": "127.0.0.1", "port": str(dead)}, timeout=1).status,
                         "fail")

    def _listener_free_port(self):
        """The port that was just opened and closed ⇒ has no listener."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        return port

    def test_service_falls_back_to_socket_without_pg_isready(self):
        """Verifies service falls back to socket without pg isready."""
        port = self._listener()
        cfg = {"host": "127.0.0.1", "port": str(port), "dbname": "", "user": ""}
        with mock.patch.object(self.t.shutil, "which", return_value=None):
            res = self.t.check_service(cfg)
        self.assertEqual(res.status, "ok")
        self.assertIn("pg_isready", res["detail"])

    def test_service_uses_pg_isready_when_present(self):
        """Verifies service uses pg isready when present."""
        cfg = {"host": "h", "port": "1", "dbname": "d", "user": "u"}
        log = []

        def fake_run(cmd, **k):
            """Helper: fake run."""
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
    """Tests for dsn and head with stub."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.t = _tool()
        self.cfg = self.t.parse_dsn("host=h dbname=d user=u")

    def test_dsn_green_and_logs_the_probe(self):
        """Verifies dsn green and logs the probe."""
        log = []
        rows = {"SELECT 1": [[1]],
                "CURRENT_DATABASE": [("d", "PostgreSQL 16.15 on x86_64")]}
        res = self.t.check_dsn(self.cfg, connect=_fake_connect(rows, log))
        self.assertEqual(res.status, "ok", res["detail"])
        self.assertIn("SELECT 1", log[0])
        self.assertIn("PostgreSQL 16.15", res["detail"])

    def test_dsn_error_is_red_with_action(self):
        """Verifies dsn error is red with action."""
        def boom(cfg, timeout=5):
            """Helper: boom."""
            raise OSError('connection to server at "h" failed')

        res = self.t.check_dsn(self.cfg, connect=boom)
        self.assertEqual(res.status, "fail")
        self.assertIn("psql", res["action"])
        self.assertIn("connection to server", res["detail"])

    def test_head_matches_repo_head(self):
        """Verifies head matches repo head."""
        log = []
        rows = {"CURRENT_SCHEMA": [("public", '"$user", public')],
                "ALEMBIC_VERSION": [("0008",)]}
        res = self.t.check_alembic_head(self.cfg, connect=_fake_connect(rows, log),
                                        versions_dir=ROOT / "alembic" / "versions")
        self.assertEqual(res.status, "ok", res["detail"])
        self.assertIn("0008", res["detail"])
        self.assertIn('search_path="$user", public', res["detail"])
        self.assertTrue(any("FROM alembic_version" in q for q in log))

    def test_head_mismatch_is_red_and_names_expected(self):
        """Verifies head mismatch is red and names expected."""
        rows = {"CURRENT_SCHEMA": [("public", "public")], "ALEMBIC_VERSION": [("0003",)]}
        res = self.t.check_alembic_head(self.cfg, connect=_fake_connect(rows, []),
                                        versions_dir=ROOT / "alembic" / "versions")
        self.assertEqual(res.status, "fail")
        self.assertIn("انتظار مخزن: 0008", res["detail"])
        self.assertIn("upgrade head", res["action"])

    def test_head_empty_table_is_red(self):
        """Verifies head empty table is red."""
        rows = {"CURRENT_SCHEMA": [("public", "public")], "ALEMBIC_VERSION": []}
        res = self.t.check_alembic_head(self.cfg, connect=_fake_connect(rows, []),
                                        versions_dir=ROOT / "alembic" / "versions")
        self.assertEqual(res.status, "fail")
        self.assertIn("هیچ سطری ندارد", res["detail"])

    def test_head_falls_back_to_public_schema(self):
        """If search_path does not find the table, it tries `public.alembic_version` (TD-011)."""
        log = []
        rows = {"CURRENT_SCHEMA": [("x", "x")], "PUBLIC.ALEMBIC_VERSION": [("0008",)]}
        connect = _fake_connect(rows, log, raise_for=("FROM alembic_version",))
        res = self.t.check_alembic_head(self.cfg, connect=connect,
                                        versions_dir=ROOT / "alembic" / "versions")
        self.assertEqual(res.status, "ok", res["detail"])
        self.assertTrue(any("public.alembic_version" in q for q in log))
        self.assertIn("public (fallback)", res["detail"])

    def test_head_without_versions_dir_is_red(self):
        """Verifies head without versions dir is red."""
        with tempfile.TemporaryDirectory() as td:
            res = self.t.check_alembic_head(self.cfg, connect=_fake_connect({}, []),
                                            versions_dir=Path(td))
        self.assertEqual(res.status, "fail")
        self.assertIn("سرشاخه", res["detail"])


class CliTests(unittest.TestCase):
    """Command line behavior: exit code, skips, and writing nothing but `--json'."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.t = _tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "logs.db"
        _make_logs_db(self.db, ("2026-10-04T10:00:00Z",))

    def _run(self, *argv):
        """Helper: run."""
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.t.main(list(argv))
        return code, buf.getvalue()

    def test_self_test_is_zero(self):
        """Verifies self test is zero."""
        code, out = self._run("--self-test")
        self.assertEqual(code, 0)
        self.assertIn("self-test", out)

    def test_skip_pg_marks_four_skips_and_writes_nothing(self):
        """Verifies skip pg marks four skips and writes nothing."""
        before = sorted(p.name for p in self.dir.iterdir())
        code, out = self._run("--db", str(self.db), "--skip-pg", "--color", "never")
        self.assertEqual(out.count("[–]"), 4)
        self.assertIn("چک‌لیست آمادگی cutover", out)
        self.assertIn(code, (self.t.EXIT_OK, self.t.EXIT_WARN))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

    def test_json_report_masks_password_and_has_ten_rows(self):
        """Verifies json report masks password and has ten rows."""
        out_file = self.dir / "report.json"
        with mock.patch.dict(os.environ, {"PG_DSN": "host=nohost dbname=d user=u password=pw"}):
            code, _ = self._run("--db", str(self.db), "--skip-pg", "--json", str(out_file))
        text = out_file.read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertEqual(code, data["exit_code"])
        # Attention: tmp directory name can have "pw" by accident ⇒ We check the password carefully
        self.assertNotIn("password=pw", text)
        self.assertNotIn("pw\n", text)
        self.assertIn("password=***", text)
        self.assertEqual(len(data["checks"]), 10)         # 9 gauges + 1 auxiliary marker row
        self.assertEqual(data["expected_heads"], ["0008"])
        self.assertEqual({r["key"] for r in data["checks"]},
                         {"python", "psycopg", "service", "dsn", "head", "port",
                          "integrity", "timestamps", "disk", "marker"})

    def test_broken_dsn_is_blocked_exit_one(self):
        """Without `--skip-pg` and with broken DSN ⇒ red ⇒ 1 (cutover not allowed)."""
        with mock.patch.dict(os.environ,
                             {"PG_DSN": "host=127.0.0.1 port=1 dbname=none user=none"},
                             clear=False):
            code, out = self._run("--db", str(self.db), "--color", "never")
        self.assertEqual(code, self.t.EXIT_BLOCKED)
        self.assertIn("[✘]", out)
        self.assertIn("cutover مجاز نیست", out)

    def test_strict_turns_yellow_into_one(self):
        """Verifies strict turns yellow into one."""
        db = self.dir / "pre.db"
        _make_logs_db(db, ("2026-01-05T10:00:00Z",))          # Z before cutover ⇒ yellow
        soft, out_soft = self._run("--db", str(db), "--skip-pg", "--color", "never")
        strict, out = self._run("--db", str(db), "--skip-pg", "--color", "never", "--strict")
        self.assertEqual(soft, self.t.EXIT_WARN)
        self.assertEqual(strict, self.t.EXIT_BLOCKED)
        self.assertIn("آماده با هشدار", out_soft)      # No strict: yellow, not red
        self.assertIn("مسدود", out)                   # With strict: the result line should also say red
        self.assertIn("--strict", out)

    def test_quiet_prints_single_verdict_line(self):
        """Verifies quiet prints single verdict line."""
        code, out = self._run("--db", str(self.db), "--skip-pg", "--quiet")
        self.assertIn("readiness:", out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.strip()]), 2)  # Result line + final message


@unittest.skipUnless(_PG_DSN, f"{ENV_PG_DSN} ست نشده ⇒ سنجه‌های زندهٔ PG اجرا نمی‌شوند")
class LivePgTests(unittest.TestCase):
    """Real PG: gauges 4 and 5 green (in PG suite = same conftest isolated schema)."""

    @classmethod
    def setUpClass(cls):
        """Prepare the fixtures shared by all tests in this class."""
        if importlib.util.find_spec("psycopg") is None:
            raise unittest.SkipTest("psycopg نصب نیست ⇒ سنجه‌های زندهٔ PG اجرا نمی‌شوند")
        cls.t = _tool()
        cls.cfg = cls.t.parse_dsn(_PG_DSN)

    def test_live_dsn_and_head(self):
        """Verifies live dsn and head."""
        dsn_res = self.t.check_dsn(self.cfg)
        if dsn_res.status != "ok":
            self.skipTest(f"PG در دسترس نیست: {dsn_res['detail'][:120]}")
        self.assertEqual(dsn_res.status, "ok")
        head = self.t.check_alembic_head(self.cfg, versions_dir=ROOT / "alembic" / "versions")
        self.assertEqual(head.status, "ok", head["detail"])
        self.assertIn("0008", head["detail"])

    def test_live_port_check(self):
        """Verifies live port check."""
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
