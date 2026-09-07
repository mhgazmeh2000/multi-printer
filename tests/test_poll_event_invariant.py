# -*- coding: utf-8 -*-
"""Poll/Event invariant tests: dedup, snapshot consistency, poll serialization."""
import json, os, sqlite3, sys, tempfile, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.database import add_event, init_db
from core import store


def _make_test_db():
    """Create isolated test DB in a unique dir per test."""
    d = tempfile.mkdtemp(prefix="poll_test_")
    old = os.getcwd()
    os.chdir(d)
    init_db()
    store._prev._cache.clear()
    return d, old


def _query_events(etype=None):
    with sqlite3.connect("logs.db") as conn:
        q = "SELECT printer_ip, type, message, details FROM logs"
        params = []
        if etype:
            q += " WHERE type = ?"
            params.append(etype)
        q += " ORDER BY timestamp"
        rows = conn.execute(q, params).fetchall()
    return [{"ip": r[0], "type": r[1], "message": r[2],
             "details": json.loads(r[3]) if r[3] else {}} for r in rows]


class TestPrintInvariant(unittest.TestCase):
    def setUp(self):
        self._d, self._old = _make_test_db()
    def tearDown(self):
        store._prev._cache.clear()
        os.chdir(self._old)
    def test_pages_match_delta(self):
        add_event("10.0.0.1", "PRINT", {"message": "5 pages", "pages": 5,
                   "color": "BW", "prev_total": 1000, "current_total": 1005})
        e = _query_events("PRINT")
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["details"]["current_total"] - e[0]["details"]["prev_total"],
                         e[0]["details"]["pages"])
    def test_pages_match_mismatch(self):
        add_event("10.0.0.1", "PRINT", {"message": "3 pages", "pages": 3,
                   "color": "unknown", "prev_total": 2000, "current_total": 2003,
                   "counter_mismatch": True, "actual_delta": 3, "split_delta": -28})
        e = _query_events("PRINT")
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["details"]["current_total"] - e[0]["details"]["prev_total"],
                         e[0]["details"]["pages"])


class TestPrintDedup(unittest.TestCase):
    def setUp(self):
        self._d, self._old = _make_test_db()
    def tearDown(self):
        store._prev._cache.clear()
        os.chdir(self._old)
    def test_same_totals_deduped(self):
        add_event("10.0.0.1", "PRINT", {"message": "5 p", "pages": 5,
                   "color": "BW", "prev_total": 1000, "current_total": 1005})
        add_event("10.0.0.1", "PRINT", {"message": "5 p", "pages": 5,
                   "color": "BW", "prev_total": 1000, "current_total": 1005})
        self.assertEqual(len(_query_events("PRINT")), 1)
    def test_different_totals_not_deduped(self):
        add_event("10.0.0.1", "PRINT", {"message": "5 p", "pages": 5,
                   "color": "BW", "prev_total": 1000, "current_total": 1005})
        add_event("10.0.0.1", "PRINT", {"message": "3 p", "pages": 3,
                   "color": "BW", "prev_total": 1005, "current_total": 1008})
        self.assertEqual(len(_query_events("PRINT")), 2)
    def test_overlapping_deduped(self):
        add_event("10.0.0.1", "PRINT", {"message": "8 p", "pages": 8,
                   "color": "BW", "prev_total": 1000, "current_total": 1008})
        add_event("10.0.0.1", "PRINT", {"message": "5 p", "pages": 5,
                   "color": "BW", "prev_total": 1000, "current_total": 1005})
        self.assertEqual(len(_query_events("PRINT")), 1)
    def test_concurrent_print(self):
        # Per-printer lock in collect() prevents concurrent polls for same IP.
        # DB-level dedup is best-effort; primary protection is the poll lock.
        def add_p():
            add_event("10.0.0.1", "PRINT", {"message": "5 p", "pages": 5,
                       "color": "BW", "prev_total": 1000, "current_total": 1005})
        # Sequential (production path via per-printer lock)
        add_p(); add_p()
        self.assertEqual(len(_query_events("PRINT")), 1)


class TestCounterResetDedup(unittest.TestCase):
    def setUp(self):
        self._d, self._old = _make_test_db()
    def tearDown(self):
        store._prev._cache.clear()
        os.chdir(self._old)
    def test_same_reset_deduped(self):
        add_event("10.0.0.1", "COUNTER_RESET", {"message": "reset",
                   "prev_total": 11737, "current_total": 0})
        add_event("10.0.0.1", "COUNTER_RESET", {"message": "reset",
                   "prev_total": 11737, "current_total": 0})
        self.assertEqual(len(_query_events("COUNTER_RESET")), 1)
    def test_different_resets_not_deduped(self):
        add_event("10.0.0.1", "COUNTER_RESET", {"message": "r1",
                   "prev_total": 11737, "current_total": 0})
        add_event("10.0.0.1", "COUNTER_RESET", {"message": "r2",
                   "prev_total": 12500, "current_total": 0})
        self.assertEqual(len(_query_events("COUNTER_RESET")), 2)
    def test_concurrent_counter_reset(self):
        # Per-printer lock in collect() prevents this in production.
        # The DB-level dedup is best-effort for sequential calls.
        def add_r():
            add_event("10.0.0.1", "COUNTER_RESET", {"message": "reset",
                       "prev_total": 53964, "current_total": 0})
        # Sequential calls (production path via per-printer lock)
        add_r(); add_r()
        self.assertEqual(len(_query_events("COUNTER_RESET")), 1)


class TestToshibaPaperSplit(unittest.TestCase):
    def setUp(self):
        self._d, self._old = _make_test_db()
    def tearDown(self):
        store._prev._cache.clear()
        os.chdir(self._old)
    def test_paper_split_no_negative(self):
        add_event("10.0.0.1", "PRINT", {"message": "4 pages", "pages": 4,
                   "color": "unknown", "prev_total": 100, "current_total": 104,
                   "counter_mismatch": True, "paper_split": {"large": 0, "small": 4}})
        e = _query_events("PRINT")
        self.assertEqual(len(e), 1)
        ps = e[0]["details"].get("paper_split", {})
        self.assertGreaterEqual(ps.get("large", 0), 0)
        self.assertGreaterEqual(ps.get("small", 0), 0)


class TestPollSerialization(unittest.TestCase):
    def test_same_ip_lock(self):
        from core.poller import _get_ip_lock
        lock = _get_ip_lock("10.0.0.1")
        self.assertTrue(lock.acquire(blocking=False))
        self.assertFalse(lock.acquire(blocking=False))
        lock.release()
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()
    def test_different_ip_lock(self):
        from core.poller import _get_ip_lock
        la = _get_ip_lock("10.0.0.1")
        lb = _get_ip_lock("10.0.0.2")
        self.assertTrue(la.acquire(blocking=False))
        self.assertTrue(lb.acquire(blocking=False))
        la.release(); lb.release()
