"""Whole-transaction retry and sub-percent-cache publication regressions."""
from contextlib import contextmanager
import sqlite3
import unittest
from unittest.mock import patch, Mock

from core.db.connection import ConnectionAdapter
from core.db.exceptions import DatabaseError
from core.db.retry import retry_transaction
from core import yield_engine as engine
from core.retention_policy import purge_table


class TransactionRetryTests(unittest.TestCase):
    def test_bounded_and_only_known_rollback_states(self):
        for state in ('40001', '40P01', '57014', '23505', '08006', None):
            operation = Mock(side_effect=DatabaseError('injected', sqlstate=state))
            with patch('core.db.retry.time.sleep'), self.assertRaises(DatabaseError):
                retry_transaction(operation)
            self.assertEqual(operation.call_count, 3 if state in ('40001', '40P01') else 1)

    def test_cache_published_after_success_and_restored_between_attempts(self):
        key = ('192.0.2.248', 'black')
        old = (10, 91.5, 'old')
        new = (20, 90.5, 'new')
        engine._EXACT_ANCHOR_CACHE[key] = old
        seen = []
        @engine._retry_yield_write
        def operation(**kwargs):
            seen.append((engine._exact_anchor_cache().get(key), kwargs['timestamp']))
            engine._exact_anchor_cache()[key] = new
            if len(seen) < 3:
                raise DatabaseError('commit conflict', sqlstate='40001')
            # Still not published, even in the final successful attempt.
            self.assertEqual(engine._EXACT_ANCHOR_CACHE[key], old)
            return 'committed'
        try:
            with patch('core.db.retry.time.sleep'):
                self.assertEqual(operation(ip=key[0], color='BLACK'), 'committed')
            self.assertEqual([entry[0] for entry in seen], [old]*3)
            self.assertEqual(len({entry[1] for entry in seen}), 1)
            self.assertEqual(engine._EXACT_ANCHOR_CACHE[key], new)
            self.assertIsNone(engine._EXACT_ATTEMPT_CACHE.get())
        finally:
            engine._EXACT_ANCHOR_CACHE.pop(key, None)

    def test_exhaustion_never_publishes_cache(self):
        key = ('192.0.2.249', 'cyan')
        @engine._retry_yield_write
        def operation(**kwargs):
            engine._exact_anchor_cache()[key] = (1, 2., 'failed')
            raise DatabaseError('conflict', sqlstate='40P01')
        with patch('core.db.retry.time.sleep'), self.assertRaises(DatabaseError):
            operation(ip=key[0], color='cyan')
        self.assertNotIn(key, engine._EXACT_ANCHOR_CACHE)
        self.assertIsNone(engine._EXACT_ATTEMPT_CACHE.get())

    def test_retention_commit_failure_replays_only_uncommitted_batch(self):
        raw = sqlite3.connect(':memory:')
        raw.execute('CREATE TABLE toner_snapshots_v2(id INTEGER PRIMARY KEY, timestamp TEXT)')
        raw.executemany('INSERT INTO toner_snapshots_v2 VALUES (?,?)', [(i, '2000-01-01') for i in range(5)])
        raw.commit()
        class FailingCommit(ConnectionAdapter):
            commits = 0
            rollbacks = 0
            def commit(self):
                self.commits += 1
                if self.commits == 2:  # first batch durable, second must replay
                    raise DatabaseError('injected', sqlstate='40001')
                super().commit()
            def rollback(self):
                self.rollbacks += 1
                super().rollback()
        conn = FailingCommit(raw, backend='sqlite')
        try:
            with patch('core.db.retry.time.sleep'):
                self.assertEqual(purge_table(conn, 'toner_snapshots_v2', days=1, batch_size=2), 5)
            self.assertEqual(conn.rollbacks, 1)
            self.assertEqual(conn.commits, 4)
            self.assertEqual(raw.execute('SELECT count(*) FROM toner_snapshots_v2').fetchone()[0], 0)
        finally:
            conn.close()

    def test_sqlite_flag_does_not_add_savepoints_or_schema_probes(self):
        raw = sqlite3.connect(':memory:')
        from tests.test_yield_query_lab import DDL
        raw.execute(DDL)
        conn = ConnectionAdapter(raw, backend='sqlite')
        trace = []
        raw.set_trace_callback(trace.append)
        try:
            with patch.dict('os.environ', {'YIELD_DERIVED_RUNS_ENABLED': '1'}):
                self.assertEqual(engine._load_interval_snapshots(conn), [])
            self.assertEqual(len(trace), 1)
            self.assertNotIn('yield_run_', trace[0])
        finally:
            conn.close()

    def test_optional_estimator_query_does_not_swallow_retryable_error(self):
        conn = Mock()
        conn.execute.side_effect = DatabaseError('conflict', sqlstate='40001')
        for operation in (
            lambda: engine._robust_learned_yield(conn, 'ip', 'black', 'key'),
            lambda: engine._recent_accepted_estimates(conn, 'ip', 'black', 'key'),
            lambda: engine._detect_regime_shift(conn, 'ip', 'black', 'key', rated_capacity=1000),
            lambda: engine.estimate_cartridge_interval(conn, ip='ip', color='black'),
        ):
            with self.assertRaises(DatabaseError):
                operation()
