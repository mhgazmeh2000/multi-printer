"""Diagnostics must not load app settings, infer PG_DSN or open a writable SQLite DB."""
import contextlib
import io
import json
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import diagnose_yield_history as diagnostic
from tools.yield_query_lab import loader_query
from tests.test_yield_query_lab import DDL, fixture, oracle


class HistoryDiagnosticTests(unittest.TestCase):
    def test_legacy_candidate_and_independent_oracle_match_filters_limits(self):
        with sqlite3.connect(':memory:') as conn:
            conn.execute(DDL)
            rows = fixture(712, 3500)
            conn.executemany('INSERT INTO toner_snapshots_v2 VALUES (' + ','.join(['?'] * 13) + ')', rows)
            for limit in (0, 1, 3, 400, 1000):
                for choice in ({}, {'ip': '192.0.2.1'}, {'color': 'BLACK'},
                               {'ip': '192.0.2.2', 'color': 'cyan'}, {'ip': 'missing'}):
                    with self.subTest(limit=limit, choice=choice):
                        sql, params = loader_query(backend='sqlite', limit=limit, **choice)
                        self.assertEqual(sql.count('?'), len(params))
                        old = conn.execute(diagnostic.legacy_query(sql), params).fetchall()
                        new = conn.execute(sql, params).fetchall()
                        self.assertEqual(old, new)
                        self.assertEqual(new, oracle(rows, limit=limit, **choice))

    def test_sqlite_cli_preserves_file_and_never_calls_application_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history #1.db'
            with sqlite3.connect(path) as conn:
                conn.execute(DDL.replace('TEMP TABLE', 'TABLE'))
                rows = fixture(7, 100)
                conn.executemany('INSERT INTO toner_snapshots_v2 VALUES (' + ','.join(['?']*13) + ')', rows)
            before = path.read_bytes()
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.dict(os.environ, {'PG_DSN': 'do not connect'}), \
                    patch('psycopg.connect', side_effect=AssertionError('no PG connection')):
                result = diagnostic.main(['--sqlite', str(path), '--repeats', '1'])
            self.assertEqual(result, 0, output.getvalue())
            report = json.loads(output.getvalue())
            self.assertIs(report['exact_ordered_equal'], True)
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn('192.0.2.', output.getvalue())

    def test_sqlite_connection_is_actually_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.db'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE source(id integer)')
            def attempt_write(execute, **kwargs):
                execute('CREATE TABLE forbidden(id integer)', ())
            output = io.StringIO()
            with patch.object(diagnostic, 'compare_queries', side_effect=attempt_write), contextlib.redirect_stdout(output):
                self.assertEqual(diagnostic.main(['--sqlite', str(path)]), 1)
            with sqlite3.connect(path) as conn:
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='forbidden'").fetchone())

    def test_copy_credentials_cannot_override_fixed_target_with_hostaddr_or_service(self):
        auth = dict(host='127.0.0.1', port=55439, user='yield_activation_lab',
                    dbname='yield_activation_copy', password='lab-only')
        diagnostic.validate_copy_auth(auth)
        for key, value in [('host', 'localhost'), ('port', 5432), ('hostaddr', '192.0.2.1'),
                           ('service', 'production'), ('dbname', 'production')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                diagnostic.validate_copy_auth({**auth, key: value})

    def test_live_copy_identity_and_readonly_must_both_match(self):
        conn = Mock()
        valid = ['127.0.0.1', 55439, 'yield_activation_copy', 'yield_activation_lab',
                 '/tmp/owned-copy', '12345', 'on']
        conn.execute.return_value.fetchone.return_value = tuple(valid)
        diagnostic.verify_copy(conn, '12345', Path('/tmp/owned-copy'))
        for index, value in enumerate(['other', 5432, 'other', 'other', '/tmp/production', 'wrong-id', 'off']):
            row = valid.copy()
            row[index] = value
            conn.execute.return_value.fetchone.return_value = tuple(row)
            with self.subTest(index=index), self.assertRaises(ValueError):
                diagnostic.verify_copy(conn, '12345', Path('/tmp/owned-copy'))

    def test_pg_identity_failure_closes_connection_without_exposing_driver_text(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = Path(folder) / 'copy-auth.json'
            auth.write_text(json.dumps(dict(host='127.0.0.1', port=55439,
                user='yield_activation_lab', dbname='yield_activation_copy', password='lab-only')))
            conn = Mock()
            conn.rollback.side_effect = RuntimeError('private rollback text')
            conn.close.side_effect = RuntimeError('private close text')
            output = io.StringIO()
            with patch('psycopg.connect', return_value=conn), \
                    patch.object(diagnostic, 'verify_copy', side_effect=ValueError('private identity text')), \
                    patch.object(diagnostic, 'compare_queries') as compare, contextlib.redirect_stdout(output):
                result = diagnostic.main(['--postgres-copy-auth', str(auth),
                    '--expected-system-id', '12345', '--expected-data-directory', folder])
            self.assertEqual(result, 1)
            self.assertIs(conn.read_only, True)
            compare.assert_not_called()
            conn.rollback.assert_called_once()
            conn.close.assert_called_once()
            self.assertNotIn('private', output.getvalue())
            self.assertNotIn('lab-only', output.getvalue())
            self.assertIn('cleanup_close_failed', output.getvalue())

    def test_sqlite_statement_timeout_aborts_work_without_row_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.db'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE source(id integer)')
            def expensive(execute, **kwargs):
                execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n', ()).fetchone()
            # Entry=0, deadline setup=0, per-statement deadline=0, check=0;
            # the progress callback then observes the exhausted local budget.
            ticks = iter([0, 0, 0, 0])
            output = io.StringIO()
            with patch.object(diagnostic, 'compare_queries', side_effect=expensive), \
                    patch.object(diagnostic.time, 'monotonic', side_effect=lambda: next(ticks, 2)), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(diagnostic.main(['--sqlite', str(path), '--timeout-seconds', '1']), 1)
            self.assertEqual(json.loads(output.getvalue())['error_type'], 'OperationalError')

    def test_dropping_last_endpoint_is_detected_by_equivalence(self):
        with sqlite3.connect(':memory:') as conn:
            conn.execute(DDL)
            rows = [(i, '192.0.2.1', 'black', i, 70, 70, 'poll', 1, None,
                     str(i), 'chip', None, i) for i in range(1, 10)]
            conn.executemany('INSERT INTO toner_snapshots_v2 VALUES (' + ','.join(['?']*13) + ')', rows)
            sql, params = loader_query(backend='sqlite')
            broken = sql.replace(' UNION SELECT last_id FROM picked', '')
            self.assertNotEqual(conn.execute(broken, params).fetchall(), oracle(rows))
            self.assertEqual(conn.execute(sql, params).fetchall(), oracle(rows))

    def test_template_drift_refuses_silent_comparison(self):
        with self.assertRaises(ValueError):
            diagnostic.legacy_query('SELECT 1')
