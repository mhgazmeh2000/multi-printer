# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_backup_pg.py
# @brief:        Tests for PostgreSQL backups with pg_dump and dump verification.
# @details:      Responsibilities: Check the dump command, password handling and TOC verification.
#                Main API: SqliteBackupOnlyTests, PgBackendEnvTests, VerifyDumpTests,
#                PgBackupCliTests
#                Side effects: database access, runs external processes, file I/O
# @dependencies: external: psycopg
# @notes:        Can be run directly as a script.
# =============================================================================

r"""
E.6.2 tests — PostgreSQL backup with `pg_dump -Fc` + verification `pg_restore --list`.

Why are these tests?
------------------
The finding of E.4.11 (clause 7.1 `docs/postgresql-strictness.md`) was that "backup" on PG
can be silent **none**: `_make_backup` relies on `shutil.copy2(DB_PATH, …)`
and when `DB_PATH` was not a file, it returns an empty string and the same program
"backed up" was reported. Unification of that lesson in the time migration tool:

  • SQLite — today's behavior (physical copy + sha comparison) intact;
  • PG — `pg_dump --format=custom' and **confirm** with `pg_restore --list'; If pg_dump
    not, the dump is empty, or the expected table is not in it ⇒ explicit error and stop.

Domain note: The rewriting of timestamps in this tool is reading/writing the SQLite file;
So on PG this tool takes a "restorable backup of the live database" - not a migration
PG data (that is the `tools/migrate_sqlite_to_pg` task in phase F).

Three things run without PG (SQLite mode, reject invalid dump, no binary) to
The normal suite should also have a cover; Two PG items with `skipUnless' and skip message on.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ENV_PG_DSN = "PG_DSN"

_PG_DSN = (os.getenv(ENV_PG_DSN, "") or "").strip()


def _pg_target():
    """``(dsn, None)'' if PG is available; Otherwise ``(None, skip reason)''."""
    if not _PG_DSN:
        return None, (f"{ENV_PG_DSN} ست نشده ⇒ تست‌های بکاپ PG این فایل اجرا نمی‌شوند")
    try:
        import psycopg
        with psycopg.connect(_PG_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:                       # noqa: BLE001
        return None, f"PG در دسترس نیست ({type(exc).__name__}) ⇒ بکاپ PG آزموده نشد"
    return _PG_DSN, None


_PG_TARGET, _PG_SKIP = _pg_target()


def _tool():
    """Loads the tools module with importlib (tools/ is not closed)."""
    path = ROOT / "tools" / "migrate_timestamps_to_utc.py"
    spec = importlib.util.spec_from_file_location("migrate_ts_tool_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path: str) -> str:
    """Helper: sha."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _make_sqlite(path: str) -> None:
    """Helper: make sqlite."""
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, timestamp TEXT)")
        conn.execute("INSERT INTO logs (timestamp) VALUES ('2026-10-04T00:00:00.000000Z')")
        conn.commit()


class SqliteBackupOnlyTests(unittest.TestCase):
    """SQLite mode: copy only, no writing to DB."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "logs.db")
        _make_sqlite(self.db)
        self.m = _tool()
        # The backup tool understands the PG from the **environment** (DB_BACKEND + PG_DSN); This behavior class
        # SQLite searches, so we lock the environment, otherwise the result to the executing shell
        # becomes dependent (the same trap that E.5.3 recorded for high-level tests).
        env = mock.patch.dict(os.environ, {"DB_BACKEND": "sqlite", "PG_DSN": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_backup_only_copies_and_touches_nothing(self):
        """Verifies backup only copies and touches nothing."""
        sha_before = _sha(self.db)
        rc = self.m.main(["--db", self.db, "--backup-only", "--quiet"])
        self.assertEqual(rc, 0)
        self.assertEqual(_sha(self.db), sha_before, "DB نباید دست بخورد")
        backups = [f for f in os.listdir(self.tmp.name) if f.endswith(".e3bak")]
        self.assertEqual(len(backups), 1, os.listdir(self.tmp.name))
        self.assertEqual(_sha(os.path.join(self.tmp.name, backups[0])), sha_before,
                         "بکاپ باید بایت‌به‌بایت همان فایل باشد")

    def test_dry_run_still_writes_no_backup(self):
        # --cutover is required, otherwise the tool will "no cutover signal available" before doing anything
        # returns (the same precheck behavior that is locked in E.3) and does not reach the backup stage.
        """Verifies dry run still writes no backup."""
        rc = self.m.main(["--db", self.db, "--dry-run", "--quiet",
                          "--cutover", "2026-01-01T00:00:00Z"])
        self.assertEqual(rc, 0)
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.endswith(".e3bak")], [],
                         "--dry-run نباید بکاپ بسازد (همان قرارداد پیش از E.6.2)")


@unittest.skipUnless(shutil.which("pg_dump"), "pg_dump نصب نیست")
class PgBackendEnvTests(unittest.TestCase):
    """``--backup-only'' should take a dump — not a file copy — when the program is on the PG."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "logs.db")
        _make_sqlite(self.db)
        self.m = _tool()

    @unittest.skipUnless(_PG_TARGET, _PG_SKIP or "")
    def test_pg_env_takes_dump_instead_of_file_copy(self):
        """Verifies pg env takes dump instead of file copy."""
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql",
                                          "PG_DSN": _PG_TARGET}):
            rc = self.m.main(["--db", self.db, "--backup-only",
                              "--backup-out", self.tmp.name, "--quiet"])
        self.assertEqual(rc, 0)
        listing = os.listdir(self.tmp.name)
        dumps = [f for f in listing if f.endswith(".dump")]
        self.assertEqual(len(dumps), 1, f"بکاپ PG ساخته نشد: {listing}")
        self.assertGreater(os.path.getsize(os.path.join(self.tmp.name, dumps[0])), 0)
        self.assertEqual([f for f in listing if f.endswith(".e3bak")], [],
                         "روی PG نباید کپیِ فایلِ بی‌ارزش («بکاپ» جعلی E.4.11) ساخته شود")

    def test_pg_env_without_dump_capability_stops(self):
        """Absence of binary on PG ⇒ stop, not continue without backup (lesson E.4.11)."""
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql",
                                          "PG_DSN": "host=127.0.0.1 port=1 dbname=none user=none"}):
            rc = self.m.main(["--db", self.db, "--backup-only", "--quiet"])
        self.assertEqual(rc, self.m.EXIT_ERROR)
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.endswith((".dump", ".e3bak"))],
                         [], "نباید چیزی به‌عنوان بکاپ باقی بماند")


    @unittest.skipUnless(_PG_TARGET, _PG_SKIP or "")
    def test_dump_lands_next_to_the_db_not_in_the_cwd(self):
        """
        The default backup folder is "beside the database".

        This stipulation came from a real self-tester: when the default was `os.getcwd()`, the suite
        PG dumps 26 files at the root of the repository (outside `logs.db-*` in.gitignore).
        """
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(elsewhere.name)
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql",
                                          "PG_DSN": _PG_TARGET}):
            rc = self.m.main(["--db", self.db, "--backup-only", "--quiet"])
        self.assertEqual(rc, 0)
        dumps = [f for f in os.listdir(self.tmp.name) if f.endswith(".dump")]
        self.assertEqual(len(dumps), 1, "dump باید کنار فایل دیتابیس بنشیند")
        self.assertEqual([f for f in os.listdir(elsewhere.name) if f.endswith(".dump")], [],
                         "پوشهٔ جاری نباید آلوده شود")


class VerifyDumpTests(unittest.TestCase):
    """Dump verification should be ruthless: anything other than a valid dump ⇒ explicit error."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = _tool()

    def test_text_file_is_rejected(self):
        """Verifies text file is rejected."""
        bad = os.path.join(self.tmp.name, "not-a-dump.dump")
        Path(bad).write_text("-- just sql\n", encoding="utf-8")
        with self.assertRaises(self.m.BackupError) as ctx:
            self.m.verify_pg_dump(bad)
        self.assertIn("pg_restore", str(ctx.exception))

    def test_empty_file_is_rejected_before_calling_pg_restore(self):
        """Verifies empty file is rejected before calling pg restore."""
        empty = os.path.join(self.tmp.name, "empty.dump")
        Path(empty).write_bytes(b"")
        with self.assertRaises(self.m.BackupError):
            self.m.verify_pg_dump(empty)

    def test_missing_binary_says_what_to_install(self):
        """Verifies missing binary says what to install."""
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(self.m.BackupError) as ctx:
                self.m.take_pg_backup("host=invalid dbname=x", self.tmp.name)
        self.assertIn("postgresql-client", str(ctx.exception))

    def _fake_run(self, stdout, *, calls=None, make_dump=False):
        """Placing `subprocess.run` in the tool module; Makes the dump file real."""
        def run(argv, **kw):
            """Helper: run."""
            if calls is not None:
                calls.append((list(argv), kw))
            if make_dump and "--file" in argv:      # Just call pg_dump
                out = argv[argv.index("--file") + 1]
                Path(out).write_bytes(b"PGDUMP-not-empty")
            return type("Proc", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()
        return mock.patch.object(self.m.subprocess, "run", side_effect=run)

    def test_empty_toc_listing_is_rejected(self):
        """A dump without a table (wrong schema/search_path) should not be considered "successful"."""
        dump = os.path.join(self.tmp.name, "empty-toc.dump")
        Path(dump).write_bytes(b"PGDUMP")
        with self._fake_run("; no objects here\n"):
            with self.assertRaises(self.m.BackupError) as ctx:
                self.m.verify_pg_dump(dump)
        self.assertIn("هیچ جدولی", str(ctx.exception))

    def test_table_data_lines_alone_do_not_count_as_tables(self):
        """The ``TABLE DATA'' line should not be counted, otherwise the table name will come out wrong."""
        dump = os.path.join(self.tmp.name, "mixed.dump")
        Path(dump).write_bytes(b"PGDUMP")
        toc = ("237; 1259 51111 TABLE public logs printguard\n"
               "238; 0 51111 TABLE DATA public logs printguard\n")
        with self._fake_run(toc):
            entries = self.m.verify_pg_dump(dump)
            self.assertEqual(entries, ["logs"], "فقط سطر TAG=TABLE شمرده می‌شود")
            with self.assertRaises(self.m.BackupError) as ctx:
                self.m.verify_pg_dump(dump, expect_tables=("logs", "users"))
        self.assertIn("users", str(ctx.exception))

    @unittest.skipUnless(shutil.which("pg_restore"), "pg_restore نصب نیست")
    def test_take_pg_backup_refuses_and_removes_an_unverifiable_dump(self):
        """
        This test will be red if `take_pg_backup` does not call the verification step.

        dummy pg_dump "success" but writes a text file; pg_restore real it
        Rejects ⇒ Backup should be declared "failed", not a junk file with
        "backed up" label should be delivered (same as E.4.11).
        """
        real_run = subprocess.run

        def fake_run(argv, **kw):
            """Helper: fake run."""
            if str(argv[0]).endswith("pg_dump"):
                out = argv[argv.index("--file") + 1]
                Path(out).write_text("-- نه dump، نه sql معتبر\n", encoding="utf-8")
                return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            return real_run(argv, **kw)

        with mock.patch.object(self.m.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(self.m.BackupError) as ctx:
                self.m.take_pg_backup("host=h dbname=d user=u", self.tmp.name)
        self.assertIn("pg_restore", str(ctx.exception))
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.endswith(".dump")], [],
                         "dump بی‌اعتبار نباید به‌عنوان «بکاپ» روی دیسک بماند")

    def test_pg_dump_is_called_without_password_on_the_command_line(self):
        """The password should go to PGPASSWORD; It is wrong to see it in `ps' or manifest."""
        calls = []
        with self._fake_run("; nothing\n", calls=calls, make_dump=True):
            with self.assertRaises(self.m.BackupError):   # Dummy TOC is empty
                self.m.take_pg_backup("host=h dbname=d user=u password=s3cr3t-hidden",
                                      self.tmp.name)
        argv, kw = calls[0]
        self.assertIn("--format=custom", argv)
        self.assertNotIn("s3cr3t-hidden", " ".join(argv), "رمز در argv نباشد")
        self.assertNotIn("password=", argv[-1], "DSNِ منتقل‌شده به pg_dump بدون رمز باشد")
        self.assertEqual(kw["env"]["PGPASSWORD"], "s3cr3t-hidden")
        self.assertNotIn("s3cr3t-hidden", kw["env"]["_PG_DUMP_DSN"])

    def test_password_never_leaves_the_process_environment(self):
        """The password is transferred to PGPASSWORD and removed from the logable DSN."""
        env, safe = self.m._env_without_password(
            "host=h dbname=d user=u password=s3cr3t-hidden")
        self.assertEqual(env["PGPASSWORD"], "s3cr3t-hidden")
        self.assertNotIn("s3cr3t-hidden", safe)
        self.assertNotIn("s3cr3t-hidden", env["_PG_DUMP_DSN"])
        blob = json.dumps({"dsn": env["_PG_DUMP_DSN"]})
        self.assertNotIn("s3cr3t-hidden", blob, "manifest/shell نباید رمز ببیند")


@unittest.skipUnless(_PG_TARGET, _PG_SKIP or "")
class PgBackupCliTests(unittest.TestCase):
    """Actual path: pg_dump -Fc on live PG, verify TOC, log in manifest."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "logs.db")
        _make_sqlite(self.db)
        self.m = _tool()

    def test_backup_only_produces_verified_dump(self):
        """Verifies backup only produces verified dump."""
        rc = self.m.main(["--db", self.db, "--pg-dsn", _PG_TARGET,
                          "--backup-only", "--backup-out", self.tmp.name,
                          "--manifest", os.path.join(self.tmp.name, "m.json")])
        self.assertEqual(rc, 0)
        dumps = [f for f in os.listdir(self.tmp.name) if f.endswith(".dump")]
        self.assertEqual(len(dumps), 1, os.listdir(self.tmp.name))
        dump = os.path.join(self.tmp.name, dumps[0])
        tables = self.m.verify_pg_dump(dump, expect_tables=("logs", "users"))
        self.assertIn("logs", tables)
        manifest = json.loads(Path(os.path.join(self.tmp.name, "m.json")).read_text(encoding="utf-8"))
        info = manifest["pg_backup"]
        self.assertEqual(info["bytes"], os.path.getsize(dump))
        self.assertEqual(info["sha256"], _sha(dump), "sha manifest باید خودِ فایل باشد")
        self.assertGreaterEqual(info["toc_entries"], 1)

    def test_a_failing_backup_stops_the_tool(self):
        """Any failed backup is not considered "taken" — execution must stop."""
        boom = self.m.BackupError("pg_dump ناموفق بود: demo")
        sha_before = _sha(self.db)
        with mock.patch.object(self.m, "take_pg_backup", side_effect=boom):
            rc = self.m.main(["--db", self.db, "--pg-dsn", "host=invalid dbname=x"])
        self.assertEqual(rc, self.m.EXIT_ERROR)
        self.assertEqual(_sha(self.db), sha_before, "پس از شکست بکاپ، نباید نوشتنی شده باشد")

    def test_skip_flag_is_respected(self):
        """With --skip-pg-backup no PG backup is taken (for dry-run on copy)."""
        with mock.patch.object(self.m, "take_pg_backup",
                               side_effect=AssertionError("نباید صدا زده شود")):
            rc = self.m.main(["--db", self.db, "--skip-pg-backup", "--backup-only",
                              "--quiet"])
        self.assertEqual(rc, 0)
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.endswith(".dump")], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
