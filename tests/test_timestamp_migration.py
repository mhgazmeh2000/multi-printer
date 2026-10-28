# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_timestamp_migration.py
# @brief:        Tests for the timestamp migration tool (phase E.3).
# @details:      Responsibilities: Check conversion rules and migration runs.
#                Main API: _DbFixture, ConversionUnitTests, MigrationRunTests
#                Side effects: database access, file I/O
# @dependencies: stdlib only
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Timestamp migration tool tests (Phase E.3 — Task E.3.2).

The requested scenario is created in step E.3.2 on a temporary DB:
  • 3 rows of naive local
  • 2 rows of ``Z'' before cutover (fake label)
  • 2 rows of ``Z'' after cutover (actual UTC)
  • 1 row ``+03:30''
  • 1 broken row

and checks that all are converted to the ``...Z'' standard (left intact),
be idempotent, rollback/backup works properly and ``meta.value'' is not touched.

Tests are independent of system TZ: all interpretations are done with ``Asia/Tehran'' explicitly.
"""
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    """Loads tools as independent modules (tools folder is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "migrate_timestamps_to_utc",
        PROJECT_ROOT / "tools" / "migrate_timestamps_to_utc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()

CUTOVER = "2026-10-03T06:35:11+00:00"          # The same sign recorded in the project's logs.db
CUTOVER_DT = datetime(2026, 10, 3, 6, 35, 11, tzinfo=timezone.utc)


class _DbFixture(unittest.TestCase):
    """Temporary DB with `logs`, `security_events`, `meta` tables and scenario rows."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "logs.db")
        self.manifest = os.path.join(self.tmp.name, "manifest.json")
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE logs (id INTEGER PRIMARY KEY, printer_ip TEXT, type TEXT,
                               pages INT, details TEXT, timestamp TEXT);
            CREATE TABLE security_events (id INTEGER PRIMARY KEY, timestamp TEXT,
                                          event_type TEXT, created_at TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL,
                               updated_at TEXT NOT NULL);
        """)
        # 3 rows of naive local (Tehran time)
        conn.executemany(
            "INSERT INTO logs (id, printer_ip, type, pages, timestamp) VALUES (?,?,?,?,?)",
            [(1, "10.0.0.1", "PRINT", 10, "2026-10-01T08:51:39.937749"),
             (2, "10.0.0.2", "PRINT", 20, "2026-10-01T09:00:00.000000"),
             (3, "10.0.0.3", "REFILL", 0, "2026-10-02T23:30:00")])
        # 2 Z rows before cutover (fake label; old cartridge engine format)
        conn.executemany(
            "INSERT INTO logs (id, printer_ip, type, pages, timestamp) VALUES (?,?,?,?,?)",
            [(4, "10.0.0.4", "CARTRIDGE_CHANGED", 0, "2026-10-02T22:00:00Z"),
             (5, "10.0.0.5", "CARTRIDGE_CHANGED", 0, "2026-10-03T06:00:00Z")])
        # 2 rows of Z after cutover (real UTC; the second one the next day)
        conn.executemany(
            "INSERT INTO logs (id, printer_ip, type, pages, timestamp) VALUES (?,?,?,?,?)",
            [(6, "10.0.0.6", "STATUS", 0, "2026-10-03T07:00:00Z"),
             (7, "10.0.0.7", "STATUS", 0, "2026-10-04T10:00:00Z")])
        # 1 offset row and 1 broken row
        conn.execute("INSERT INTO logs (id, printer_ip, type, pages, timestamp) "
                     "VALUES (8, '10.0.0.8', 'PRINT', 5, '2026-10-03T11:00:00+03:30')")
        conn.execute("INSERT INTO logs (id, printer_ip, type, pages, timestamp) "
                     "VALUES (9, '10.0.0.9', 'PRINT', 5, 'not-a-timestamp')")
        # security_events: two time columns + a NULL value
        conn.executemany(
            "INSERT INTO security_events (id, timestamp, event_type, created_at) "
            "VALUES (?,?,?,?)",
            [(1, "2026-10-01T08:50:13.110514", "login", "2026-10-01T08:50:13.110514"),
             (2, "2026-10-03T09:55:51+00:00", "login_failed", "2026-10-03T09:55:51+00:00"),
             (3, None, "logout", "2026-10-01T10:00:00")])
        # meta: cutover token (must not be converted) + updated_at (must be converted)
        conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?,?,?)",
                     ("timezone_cutover_at", CUTOVER, "2026-10-03T09:55:51+00:00"))
        conn.commit()
        conn.close()

    def tearDown(self):
        """Release the resources that each test in this class created."""
        self.tmp.cleanup()

    def rows(self, table="logs", col="timestamp"):
        """Helper: rows."""
        conn = sqlite3.connect(self.db)
        try:
            return {r[0]: r[1] for r in conn.execute(f'SELECT id, "{col}" FROM {table}')}
        finally:
            conn.close()

    def run_tool(self, *extra):
        # E.6.2: The PG backup tool understands the environment; This suite on the temporary copy of SQLite
        # It works, so we lock the environment to run the PG suite (DB_BACKEND=postgresql)
        # Next to each test, take an actual pg_dump of the live database.
        """Helper: run tool."""
        with mock.patch.dict(os.environ, {"DB_BACKEND": "sqlite", "PG_DSN": ""}):
            return tool.main(["--db", self.db, "--manifest", self.manifest, *extra])


class ConversionUnitTests(unittest.TestCase):
    """Conversion rule at function level (no DB) — fast and explicit."""

    def test_naive_becomes_tehran_to_utc(self):
        """Verifies naive becomes tehran to utc."""
        new, cat, changed = tool.convert_value("2026-10-01T08:51:39.937749", CUTOVER_DT)
        self.assertEqual(new, "2026-10-01T05:21:39.937749Z")
        self.assertEqual(cat, "naive_local")
        self.assertTrue(changed)

    def test_z_before_cutover_is_fake_label(self):
        """Verifies z before cutover is fake label."""
        new, cat, changed = tool.convert_value("2026-10-03T06:00:00Z", CUTOVER_DT)
        self.assertEqual(new, "2026-10-03T02:30:00.000000Z")     # 06:00 Tehran = 02:30 UTC
        self.assertEqual(cat, "z_before_cutover")
        self.assertTrue(changed)

    def test_known_blind_spot_of_the_cutover_rule(self):
        """
        Documented blind spot: fake line written at 3:30 before cutover
        Its label passes the mark and reads "True UTC". only here
        It is **registered** so that the behavior of the code and the document remain the same (the decision of the document, not a bug of this tool).
        """
        new, cat, _ = tool.convert_value("2026-10-03T07:09:06Z", CUTOVER_DT)
        self.assertEqual(cat, "z_after_cutover")
        self.assertEqual(new, "2026-10-03T07:09:06.000000Z")

    def test_z_after_cutover_is_true_utc(self):
        """Verifies z after cutover is true utc."""
        new, cat, changed = tool.convert_value("2026-10-03T07:00:00Z", CUTOVER_DT)
        self.assertEqual(new, "2026-10-03T07:00:00.000000Z")
        self.assertEqual(cat, "z_after_cutover")
        self.assertTrue(changed)

    def test_offset_is_interpreted_as_is(self):
        """Verifies offset is interpreted as is."""
        new, cat, changed = tool.convert_value("2026-10-03T11:00:00+03:30", CUTOVER_DT)
        self.assertEqual(new, "2026-10-03T07:30:00.000000Z")
        self.assertEqual(cat, "offset_aware")

    def test_unparsable_and_empty_are_untouched(self):
        """Verifies unparsable and empty are untouched."""
        self.assertEqual(tool.convert_value("not-a-timestamp", CUTOVER_DT),
                         ("not-a-timestamp", "unparsable", False))
        self.assertEqual(tool.convert_value(None, CUTOVER_DT), (None, "empty", False))
        self.assertEqual(tool.convert_value("", CUTOVER_DT), ("", "empty", False))

    def test_already_standard_is_idempotent(self):
        """Verifies already standard is idempotent."""
        std = "2026-10-01T05:21:39.937749Z"
        new, cat, changed = tool.convert_value(std, CUTOVER_DT)
        self.assertEqual(new, std)
        self.assertEqual(cat, "already_standard")
        self.assertFalse(changed)

    def test_standard_z_before_cutover_is_not_reinterpreted(self):
        """
        Main idempotency guard: Migrated row whose time is before cutover
        Shouldn't be assumed to be "fake" again (otherwise each performance would move 3:30).
        """
        migrated = "2026-10-03T03:39:06.000000Z"     # The result of fake row migration
        self.assertLess(datetime.fromisoformat(migrated.replace("Z", "+00:00")), CUTOVER_DT)
        _, cat, changed = tool.convert_value(migrated, CUTOVER_DT)
        self.assertEqual(cat, "already_standard")
        self.assertFalse(changed)

    def test_dst_era_values_use_real_zone_not_fixed_offset(self):
        """It was summer 2022 in Tehran +04:30; Fixed offset +03:30 was ruining the result."""
        new, _, _ = tool.convert_value("2022-07-01T12:00:00", CUTOVER_DT)
        self.assertEqual(new, "2022-07-01T07:30:00.000000Z")     # 12:00 − 4:30


class MigrationRunTests(_DbFixture):

    """Tests for migration run."""
    def test_dry_run_changes_nothing_and_writes_manifest(self):
        """Verifies dry run changes nothing and writes manifest."""
        before = self.rows()
        self.assertEqual(self.run_tool("--dry-run", "--quiet"), 0)
        self.assertEqual(self.rows(), before)
        with open(self.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["mode"], "dry-run")
        self.assertFalse(os.path.exists(self.db + ".e3bak") or
                         [f for f in os.listdir(self.tmp.name) if f.endswith(".e3bak")])
        self.assertNotIn("sha256_after", manifest)

    def test_full_run_converts_all_categories(self):
        """Verifies full run converts all categories."""
        self.assertEqual(self.run_tool("--quiet"), 0)
        rows = self.rows()
        self.assertEqual(rows[1], "2026-10-01T05:21:39.937749Z")   # naive
        self.assertEqual(rows[3], "2026-10-02T20:00:00.000000Z")   # naive midnight breaker
        self.assertEqual(rows[4], "2026-10-02T18:30:00.000000Z")   # Fake Z
        self.assertEqual(rows[5], "2026-10-03T02:30:00.000000Z")   # Fake Z
        self.assertEqual(rows[6], "2026-10-03T07:00:00.000000Z")   # Real Z
        self.assertEqual(rows[8], "2026-10-03T07:30:00.000000Z")   # offset
        self.assertEqual(rows[9], "not-a-timestamp")               # Damaged: intact
        for value in rows.values():
            if value != "not-a-timestamp":
                self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

    def test_multi_column_table_and_null_values(self):
        """Verifies multi column table and null values."""
        self.assertEqual(self.run_tool("--quiet"), 0)
        seconds = self.rows("security_events", "created_at")
        self.assertEqual(seconds[1], "2026-10-01T05:20:13.110514Z")
        self.assertEqual(seconds[3], "2026-10-01T06:30:00.000000Z")
        stamps = self.rows("security_events", "timestamp")
        self.assertEqual(stamps[2], "2026-10-03T09:55:51.000000Z")

    def test_meta_value_is_protected_but_updated_at_migrates(self):
        """Verifies meta value is protected but updated at migrates."""
        self.assertEqual(self.run_tool("--quiet"), 0)
        conn = sqlite3.connect(self.db)
        try:
            value, updated = conn.execute(
                "SELECT value, updated_at FROM meta WHERE key = 'timezone_cutover_at'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(value, CUTOVER)                          # Reference: Untouched
        self.assertEqual(updated, "2026-10-03T09:55:51.000000Z")   # Data: standardized

    def test_backup_is_created_and_ignored_name(self):
        """Verifies backup is created and ignored name."""
        self.assertEqual(self.run_tool("--quiet"), 0)
        backups = [f for f in os.listdir(self.tmp.name) if f.endswith(".e3bak")]
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].startswith("logs.db-"))

    def test_second_run_is_noop(self):
        """Verifies second run is noop."""
        self.assertEqual(self.run_tool("--quiet"), 0)
        after_first = self.rows()
        with open(self.manifest, encoding="utf-8") as fh:
            first_manifest = json.load(fh)
        self.assertEqual(self.run_tool("--quiet"), 0)
        self.assertEqual(self.rows(), after_first)
        with open(self.manifest, encoding="utf-8") as fh:
            second_manifest = json.load(fh)
        self.assertEqual(second_manifest["mode"], "no-op")
        self.assertEqual(second_manifest["sha256_before"], first_manifest["sha256_after"])

    def test_missing_cutover_marker_stops_before_writing(self):
        """Verifies missing cutover marker stops before writing."""
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM meta")
        conn.commit()
        conn.close()
        before = self.rows()
        self.assertEqual(self.run_tool("--quiet"), 2)
        self.assertEqual(self.rows(), before)

    def test_unparsable_value_is_warned_but_run_succeeds(self):
        """Unreachable garbage: warning + logging in manifest, but exit code 0 (no human decision required)."""
        self.assertEqual(self.run_tool("--quiet"), 0)
        self.assertEqual(self.rows()[1], "2026-10-01T05:21:39.937749Z")
        with open(self.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["totals"]["skipped"], 1)
        self.assertEqual(manifest["totals"]["needs_decision"], 0)
        self.assertEqual(manifest["skipped_samples"][0]["value"], "not-a-timestamp")

    def test_manifest_totals_reconcile(self):
        """Verifies manifest totals reconcile."""
        self.run_tool("--quiet")
        with open(self.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        logs_entry = manifest["tables"]["logs"]
        counted = sum(sum(c.values()) for c in logs_entry["columns"].values())
        self.assertEqual(counted, logs_entry["rows"])
        self.assertEqual(manifest["totals"]["rows"],
                         sum(t.get("rows", 0) for t in manifest["tables"].values()))

    def test_write_error_rolls_back_everything(self):
        """
        If the write breaks midway, nothing changes (the entire migration is a transaction).

        ``sqlite3.Connection.execute'' is read-only, so with a thin proxy
        It is replaced which gives an error after three UPDATEs.
        """
        before = self.rows()

        class _Boom(Exception):
            """Helper class: boom."""
            pass

        real_connect = sqlite3.connect

        class _FailingConnection:
            """Helper class: failing connection."""
            def __init__(self, raw):
                """Initialize the instance."""
                self._raw = raw
                self._updates = 0

            def execute(self, sql, *params):
                """Helper: execute."""
                if sql.strip().upper().startswith("UPDATE"):
                    self._updates += 1
                    if self._updates > 3:
                        raise _Boom("شکست شبیه‌سازی‌شده")
                return self._raw.execute(sql, *params)

            def __getattr__(self, name):
                """Special method __getattr__."""
                return getattr(self._raw, name)

        def fake_connect(path, *a, **kw):
            """Helper: fake connect."""
            return _FailingConnection(real_connect(path, *a, **kw))

        with mock.patch.object(tool.sqlite3, "connect", side_effect=fake_connect):
            with self.assertRaises(_Boom):
                tool.migrate_rows(self.db, CUTOVER_DT, ["logs"])
        self.assertEqual(self.rows(), before)

    def test_needs_decision_when_z_row_has_no_marker(self):
        """Row `Z' no trusted token ⇒ exit code 2 (human decision), no writing."""
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM meta")
        conn.commit()
        conn.close()
        before = self.rows()
        self.assertEqual(self.run_tool("--cutover", "", "--quiet"), 2)
        self.assertEqual(self.rows(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
