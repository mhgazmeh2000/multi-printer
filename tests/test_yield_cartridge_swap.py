# =============================================================================
# @file:         tests/test_yield_cartridge_swap.py
# @brief:        Tests for cartridge swaps in the same printer slot (R2).
# @details:      Responsibilities: a new cartridge in an existing (printer_ip, color) slot must not inherit the
#                old cartridge's learned yield, and the robust median must only use samples of the current
#                cartridge_key (legacy rows without a key are still matched by slot, with a one-time warning).
#                Main API: CartridgeSwapTests
#                Side effects: file I/O (temporary database and catalog)
# @dependencies: internal: core.yield_engine, core.database
# @notes:        Reads environment variables: CARTRIDGE_YIELD_CATALOG. Each test runs in its own temporary
#                working directory.
# =============================================================================

"""Tests for cartridge swaps in the same slot and for the cartridge-scoped robust yield query."""

import os
import tempfile
import unittest

from core import database
from core.yield_engine import (
    DEFAULT_YIELD_PER_PAGE,
    _record_sample,
    _robust_learned_yield,
    build_cartridge_key,
    ensure_yield_tables,
    process_cartridge_snapshot,
    register_manual_refill,
)

MODEL = "Swap Test"


class CartridgeSwapTests(unittest.TestCase):
    """Tests for cartridge swaps in one printer slot."""

    def setUp(self):
        """Prepare a temporary working directory and an empty catalog."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        self.old_catalog_env = os.environ.get("CARTRIDGE_YIELD_CATALOG")
        os.chdir(self.tmp.name)
        catalog_path = os.path.join(self.tmp.name, "catalog.json")
        with open(catalog_path, "w", encoding="utf-8") as f:
            f.write('{"entries": []}')
        os.environ["CARTRIDGE_YIELD_CATALOG"] = catalog_path

    def tearDown(self):
        """Restore the environment and remove the temporary files."""
        if self.old_catalog_env is None:
            os.environ.pop("CARTRIDGE_YIELD_CATALOG", None)
        else:
            os.environ["CARTRIDGE_YIELD_CATALOG"] = self.old_catalog_env
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _poll(self, ip, cartridge, level, total, day, model=MODEL):
        """Runs one poll for the black slot of the given printer."""
        return process_cartridge_snapshot(
            ip=ip, color="black", printer_model=model, cartridge_name=cartridge,
            level=level, counters={"total": total}, device_type="mono",
            timestamp=f"2026-02-{day:02d}T00:00:00",
        )

    def _build_learned(self, ip, cartridge, drops=8, pages=500, start_day=1):
        """Creates `drops` accepted samples of 10,000 pages each (5 points per 500 pages)."""
        level, total, day = 100, 1000, start_day
        meta = self._poll(ip, cartridge, level, total, day)
        for _ in range(drops):
            level -= 5
            total += pages
            day += 1
            meta = self._poll(ip, cartridge, level, total, day)
        return meta, level, total, day

    def _sample_estimates(self, ip, cartridge_key):
        """Returns the accepted estimated_yield values stored for a cartridge key in the slot."""
        with database.db_connection() as conn:
            rows = conn.execute(
                "SELECT estimated_yield FROM yield_samples WHERE printer_ip = ? AND color = ? "
                "AND cartridge_key = ? AND accepted = ? ORDER BY id",
                (ip, "black", cartridge_key, True),
            ).fetchall()
        return [int(r[0]) for r in rows]

    # --- required tests ----------------------------------------------------

    def test_cartridge_swap_resets_learned_yield(self):
        """A new cartridge in the same slot starts from the default, not from the old cartridge's 10,000."""
        ip = "10.0.1.10"
        meta, level, total, day = self._build_learned(ip, "Toner OLD")
        self.assertEqual(meta["sample_count"], 8)
        self.assertEqual(meta["yield_per_page"], 10000)
        self.assertEqual(meta["confidence"], "high")

        day += 1
        meta = self._poll(ip, "Toner NEW", level, total + 500, day)
        self.assertEqual(meta["yield_per_page"], DEFAULT_YIELD_PER_PAGE)
        self.assertEqual(meta["yield_source"], "default")
        self.assertEqual(meta["confidence"], "low")
        self.assertEqual(meta["sample_count"], 0)
        self.assertEqual(meta["anchor_level"], level)
        self.assertEqual(meta["anchor_counter"], total + 500)

        # The first sample of the new cartridge is learned on its own: 300 pages over 10 points = 3000.
        day += 1
        meta = self._poll(ip, "Toner NEW", level - 10, total + 800, day)
        self.assertEqual(meta["sample_count"], 1)
        self.assertEqual(meta["yield_per_page"], 3000)
        self.assertEqual(meta["yield_source"], "auto_learn")

    def test_cartridge_swap_does_not_mix_samples(self):
        """The robust median for the new cartridge key ignores the old key's samples in the same slot."""
        ensure_yield_tables()
        ip, color = "10.0.1.11", "black"
        key_a = build_cartridge_key(MODEL, "Toner A", color)
        key_b = build_cartridge_key(MODEL, "Toner B", color)
        with database.db_connection(commit=True) as conn:
            for estimate in (9000, 9500, 10000):
                _record_sample(
                    conn, ip=ip, color=color, printer_model=MODEL, cartridge_name="Toner A",
                    cartridge_key=key_a, start_counter=1, end_counter=2, start_level=100, end_level=95,
                    pages_delta=500, toner_drop=5, estimated_yield=estimate, counter_basis="total",
                    weight=1.0, accepted=True, source="test",
                )
            value_b, _ = _robust_learned_yield(conn, ip, color, key_b)
            value_a, _ = _robust_learned_yield(conn, ip, color, key_a)
        self.assertIsNone(value_b)
        self.assertEqual(value_a, 9500)

    def test_legacy_rows_without_cartridge_key_still_use_fallback(self):
        """Legacy rows with no cartridge_key are still used for the slot, and a warning is logged once."""
        ensure_yield_tables()
        ip, color = "10.0.1.12", "black"
        key = build_cartridge_key(MODEL, "Toner L", color)
        with database.db_connection(commit=True) as conn:
            _record_sample(
                conn, ip=ip, color=color, printer_model=MODEL, cartridge_name="Toner L",
                cartridge_key=None, start_counter=1, end_counter=2, start_level=100, end_level=95,
                pages_delta=500, toner_drop=5, estimated_yield=7000, counter_basis="total",
                weight=1.0, accepted=True, source="test",
            )
            with self.assertLogs("PrinterMonitor", level="WARNING") as logs:
                value, _ = _robust_learned_yield(conn, ip, color, key)
            self.assertEqual(value, 7000)
            self.assertTrue(any("legacy samples" in line for line in logs.output))
            with self.assertNoLogs("PrinterMonitor", level="WARNING"):
                value, _ = _robust_learned_yield(conn, ip, color, key)
            self.assertEqual(value, 7000)

    # --- negative tests: no spurious resets ---------------------------------

    def test_legacy_state_without_key_is_not_reset(self):
        """A slot whose stored row has no cartridge_key keeps its learned state when the key appears."""
        ip = "10.0.1.13"
        meta, level, total, day = self._build_learned(ip, "Toner Same", drops=3)
        self.assertEqual(meta["sample_count"], 3)
        yield_before = meta["yield_per_page"]
        with database.db_connection(commit=True) as conn:
            conn.execute("UPDATE cartridge_state SET cartridge_key = NULL WHERE printer_ip = ?", (ip,))
        day += 1
        meta = self._poll(ip, "Toner Same", level - 5, total + 500, day)
        self.assertEqual(meta["sample_count"], 4)
        self.assertEqual(meta["yield_per_page"], yield_before)

    def test_metadata_completion_is_not_a_swap(self):
        """Model text going from 'Unknown' to a real model is metadata completion and does not reset."""
        ip = "10.0.1.14"
        self._poll(ip, "Toner X", 100, 1000, 1, model="Unknown")
        meta = self._poll(ip, "Toner X", 95, 1500, 2, model="Unknown")
        self.assertEqual(meta["sample_count"], 1)
        meta = self._poll(ip, "Toner X", 90, 2000, 3, model=MODEL)
        self.assertEqual(meta["sample_count"], 2)
        self.assertNotEqual(meta["yield_source"], "default")

    def test_manual_reset_with_new_cartridge_restarts_learning(self):
        """A manual reset with a different cartridge does not inherit the old yield or sample count."""
        ip = "10.0.1.15"
        meta, level, total, day = self._build_learned(ip, "Toner OLD2")
        self.assertEqual(meta["yield_per_page"], 10000)
        day += 1
        meta = register_manual_refill(
            ip=ip, color="black", printer_model=MODEL, cartridge_name="Toner NEW2", new_level=100,
            counters={"total": total + 500}, device_type="mono", timestamp=f"2026-02-{day:02d}T00:00:00",
        )
        self.assertEqual(meta["yield_per_page"], DEFAULT_YIELD_PER_PAGE)
        self.assertEqual(meta["sample_count"], 0)
        self.assertEqual(meta["yield_source"], "default")
        # The old cartridge's cycle was not closed under the new key.
        new_key = build_cartridge_key(MODEL, "Toner NEW2", "black")
        self.assertEqual(self._sample_estimates(ip, new_key), [])


if __name__ == "__main__":
    unittest.main()
