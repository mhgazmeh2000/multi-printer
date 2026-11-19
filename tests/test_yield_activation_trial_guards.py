"""Copy trial guards plus an optional owned-local PostgreSQL rehearsal."""
import io
import os
import subprocess
import zipfile
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import trial_yield_activation_copy as trial


class TrialGuards(unittest.TestCase):
    def report(self):
        return dict(status='RESTORE_VERIFIED', cluster_stopped=True,
                    source_commit=trial.HEAD, system_identifier=trial.SYSTEM_ID,
                    snapshot_rows=2256043)

    def auth(self):
        return dict(host='127.0.0.1', port=trial.PORT, user=trial.USER,
                    dbname=trial.DB, password='local-test-only')

    def test_copy_target_accepted(self):
        trial.validate_report(self.report(), self.auth())

    def test_changed_report_refused(self):
        for key, value in [('status', 'FAILED'), ('cluster_stopped', False),
                           ('source_commit', 'other'), ('system_identifier', 'other'), ('snapshot_rows', 1)]:
            with self.subTest(key=key):
                report = self.report()
                report[key] = value
                with self.assertRaisesRegex(RuntimeError, 'COPY_REPORT_MISMATCH'):
                    trial.validate_report(report, self.auth())

    def test_auth_cannot_point_to_production(self):
        for key, value in [('host', 'localhost'), ('port', 5432), ('user', 'postgres'),
                           ('dbname', 'production'), ('password', '')]:
            with self.subTest(key=key):
                auth = self.auth()
                auth[key] = value
                with self.assertRaisesRegex(RuntimeError, 'COPY_AUTH_TARGET_MISMATCH'):
                    trial.validate_report(self.report(), auth)

    def test_unsafe_archive_refused(self):
        for name in ('../outside', '/absolute', 'C:/outside', 'a/.env', 'a\\..\\outside'):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'UNSAFE_SOURCE_ARCHIVE'):
                trial.safe_archive_names([name])
        trial.safe_archive_names(['core/yield_engine.py', '.env.example'])

    def test_archive_respects_git_export_line_endings(self):
        # Self-contained: CI commonly checks out shallow history without the
        # pinned production commit. Reproduce export conversion in an owned repo.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.PIPE)
            git('init', '-q')
            (root / '.gitattributes').write_bytes(b'* text=auto eol=lf\n*.bat text eol=crlf\n')
            (root / 'script.bat').write_bytes(b'echo ok\n')
            git('-c', 'core.autocrlf=false', 'add', '.')
            git('-c', 'user.name=Copy Test', '-c', 'user.email=copy-test@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-qm', 'owned export fixture')
            expected = git('archive', '--format=zip', 'HEAD')
            with zipfile.ZipFile(io.BytesIO(expected)) as archive:
                raw = git('show', 'HEAD:script.bat')
                self.assertNotEqual(raw, archive.read('script.bat'))
                trial.verify_source_archive(archive, expected)

    def test_changed_or_extra_archive_payload_refused(self):
        def zipped(items):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                for name, data in items:
                    archive.writestr(name, data)
            return buffer.getvalue()
        expected = zipped([('script.bat', b'echo ok\r\n')])
        for items in ([('script.bat', b'echo changed\r\n')],
                      [('script.bat', b'echo ok\r\n'), ('extra.py', b'bad')]):
            with zipfile.ZipFile(io.BytesIO(zipped(items))) as archive:
                with self.assertRaisesRegex(RuntimeError, 'SOURCE_ARCHIVE_'):
                    trial.verify_source_archive(archive, expected)

    def test_error_codes_do_not_expose_messages(self):
        self.assertEqual(trial.safe_error_code(RuntimeError('SOURCE_ARCHIVE_COMMIT_MISMATCH')),
                         'SOURCE_ARCHIVE_COMMIT_MISMATCH')
        for exc in (RuntimeError('PRIVATE_PASSWORD'), ValueError('SOURCE_ARCHIVE_COMMIT_MISMATCH')):
            self.assertEqual(trial.safe_error_code(exc), 'UNCLASSIFIED')

    def test_identity_checks_system_id_and_all_target_fields(self):
        valid = ['127.0.0.1', trial.PORT, trial.DB, trial.USER,
                 str(trial.ROOT / 'data'), trial.SYSTEM_ID]
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = tuple(valid)
        trial.assert_identity(conn)
        conn.rollback.assert_called_once()
        for i, replacement in enumerate(['other', 5432, 'production', 'other', 'other-data', 'wrong-id']):
            row = valid.copy()
            row[i] = replacement
            conn.execute.return_value.fetchone.return_value = tuple(row)
            with self.subTest(field=i), self.assertRaisesRegex(RuntimeError, 'COPY_IDENTITY_MISMATCH'):
                trial.assert_identity(conn)

    def test_wrong_os_refuses_io(self):
        with patch.object(trial.os, 'name', 'posix'), patch.object(trial.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'WINDOWS_ONLY'):
                trial.main()
            run.assert_not_called()


class AnalyzedRetryGuards(unittest.TestCase):
    def previous(self):
        return dict(status='FAILED', cluster_stopped=True, activation_committed=False,
                    disable_committed=False, baseline_seconds=[], slot_count=51,
                    source_commit=trial.HEAD, system_identifier=trial.SYSTEM_ID,
                    error_type='DatabaseOperationalError')

    def test_only_observed_stopped_baseline_failure_can_retry(self):
        trial.validate_baseline_retry(self.previous())
        for key, value in [('status', 'COPY_HISTORY_TRIAL_PASSED'), ('cluster_stopped', False),
                           ('activation_committed', True), ('disable_committed', True),
                           ('baseline_seconds', [1.0]), ('slot_count', 52), ('source_commit', 'other'),
                           ('system_identifier', 'wrong'), ('error_type', 'ValueError')]:
            with self.subTest(key=key):
                report = self.previous()
                report[key] = value
                with self.assertRaisesRegex(RuntimeError, 'BASELINE_RETRY_STATE_MISMATCH'):
                    trial.validate_baseline_retry(report)

    def test_sqlstate_output_is_restricted(self):
        exc = RuntimeError('private driver message')
        for state, expected in [('57014', '57014'), ('55P03', '55P03'), (None, 'unknown'),
                                ('secret-password', 'unknown'), ('1234\\n', 'unknown'),
                                ('１２３４５', 'unknown')]:
            exc.sqlstate = state
            self.assertEqual(trial.safe_sqlstate(exc), expected)

    def test_analyze_refuses_active_copy_without_commit_or_analyze(self):
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = (True,)
        with self.assertRaisesRegex(RuntimeError, 'COPY_NOT_INACTIVE_BEFORE_ANALYZE'):
            trial.analyze_copy(conn, {})
        conn.commit.assert_not_called()
        self.assertFalse(any(str(call.args[0]).startswith('ANALYZE ') for call in conn.execute.call_args_list))

    def test_analyze_keeps_120_second_cap_and_commits_statistics_only(self):
        conn = Mock()
        conn.execute.return_value.fetchone.side_effect = [(False,), (-1.0,), (500.0,)]
        report = {}
        trial.analyze_copy(conn, report)
        sql = [call.args[0] for call in conn.execute.call_args_list]
        self.assertIn("SET LOCAL statement_timeout = '120s'", sql)
        self.assertIn('ANALYZE public.toner_snapshots_v2', sql)
        self.assertFalse(any(statement.startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for statement in sql))
        conn.commit.assert_called_once()
        self.assertIs(report['analyze_committed'], True)
        self.assertEqual(report['planner_rows_after_analyze'], 500.0)


class LockObservationGuards(unittest.TestCase):
    def test_only_observed_lock_failure_is_eligible(self):
        previous = dict(status='FAILED', cluster_stopped=True, activation_committed=False,
                        disable_committed=False, analyze_committed=True, phase='ACTIVATION',
                        sqlstate='55P03', slot_count=51, source_commit=trial.HEAD,
                        system_identifier=trial.SYSTEM_ID, baseline_seconds=[29.9, 2.0, 5.3, 18.4])
        trial.validate_lock_retry(previous)
        for key, value in [('cluster_stopped', False), ('activation_committed', True),
                           ('disable_committed', True), ('analyze_committed', False),
                           ('phase', 'REFERENCE_READ_1'), ('sqlstate', '57014'),
                           ('system_identifier', 'other'), ('source_commit', 'other'),
                           ('slot_count', 52), ('baseline_seconds', [])]:
            bad = dict(previous, **{key: value})
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'LOCK_RETRY_STATE_MISMATCH'):
                trial.validate_lock_retry(bad)

    def test_sample_does_not_expose_sql_or_arbitrary_activity_fields(self):
        diag = Mock()
        diag.execute.return_value.fetchall.return_value = [(1, 'autovacuum worker', 'active'),
                                                            (2, 'PRIVATE', 'PRIVATE')]
        sample = trial.blocker_sample(diag, 123)
        self.assertEqual(sample[0]['backend_type'], 'autovacuum worker')
        self.assertNotIn('PRIVATE', str(sample))
        query = diag.execute.call_args.args[0]
        for forbidden in ('application_name', 'query,', 'client_addr', 'usename'):
            self.assertNotIn(forbidden, query)

    def test_stage_labels_are_fixed_not_raw_sql(self):
        self.assertEqual(trial.maintenance_stage('LOCK TABLE toner_snapshots_v2 IN ACCESS EXCLUSIVE MODE'),
                         'LOCK_SOURCE_EXCLUSIVE')
        self.assertEqual(trial.maintenance_stage('CREATE INDEX yield_runs_snapshot_neighbor ON t (id)'),
                         'CREATE_SOURCE_INDEX')
        self.assertEqual(trial.maintenance_stage('SELECT private_payload'), 'OTHER_MAINTENANCE_SQL')


class LocalPostgresRehearsal(unittest.TestCase):
    def test_full_maintenance_reader_update_rollback_disable(self):
        try:
            import pgserver
            import psycopg
        except ImportError:
            self.skipTest('optional owned-local PostgreSQL dependencies missing')
        from tools.yield_query_lab import isolated_libpq_environment
        from tools.yield_run_prototype import BASE_DDL
        from tests.test_yield_query_lab import fixture
        root = Path(__file__).resolve().parents[1]
        with isolated_libpq_environment(), tempfile.TemporaryDirectory() as folder, \
                patch('dotenv.load_dotenv', return_value=False), \
                patch.dict(os.environ, {'YIELD_DERIVED_RUNS_ENABLED': '0'}):
            server = pgserver.get_server(Path(folder) / 'data', cleanup_mode='delete')
            try:
                with psycopg.connect(server.get_uri()) as raw:
                    raw.execute(BASE_DDL.replace('yield_run_lab.', ''))
                    raw.execute("CREATE TABLE alembic_version(version_num text); INSERT INTO alembic_version VALUES ('0009')")
                    raw.execute((root / 'alembic/sql/0009_yield_runs.sql').read_text().replace('__APP_SCHEMA__', 'public'))
                    rows = fixture(17, 500)
                    with raw.cursor() as cursor:
                        cursor.executemany('INSERT INTO toner_snapshots_v2 VALUES (' + ','.join(['%s'] * 13) + ')', rows)
                    raw.commit()
                    from core.db.connection import ConnectionAdapter
                    report = {}
                    conn = ConnectionAdapter(raw, backend='postgresql')
                    def diagnostic_factory():
                        return psycopg.connect(server.get_uri(), autocommit=True,
                            options='-c statement_timeout=500 -c default_transaction_read_only=on')
                    original_execute = conn.execute
                    # A real second session holds a share lock. The observer must
                    # capture its PID without changing the 1s maintenance lock cap.
                    with psycopg.connect(server.get_uri()) as blocker:
                        blocker.execute('SELECT count(*) FROM toner_snapshots_v2').fetchone()
                        conn.execute("SET LOCAL lock_timeout = '1s'")
                        failed = {}
                        with self.assertRaises(Exception) as failure:
                            with trial.observe_activation(conn, failed, diagnostic_factory):
                                conn.execute('LOCK TABLE toner_snapshots_v2 IN ACCESS EXCLUSIVE MODE')
                        self.assertEqual(trial.safe_sqlstate(failure.exception), '55P03')
                        self.assertEqual(failed['activation_failed_sql_stage'], 'LOCK_SOURCE_EXCLUSIVE')
                        samples = failed['activation_lock_observer']['samples']
                        self.assertTrue(any(entry['pid'] == blocker.info.backend_pid
                                            for sample in samples for entry in sample['blockers']))
                        self.assertEqual(failed['activation_lock_observer']['status'], 'STOPPED')
                        self.assertEqual(conn.execute, original_execute)
                        conn.rollback()
                    trial.analyze_copy(conn, report)
                    self.assertIs(report['analyze_committed'], True)
                    observer = lambda connection, evidence: trial.observe_activation(connection, evidence, diagnostic_factory)
                    trial.execute_trial(conn, root, report, expected_rows=len(rows), activation_observer=observer)
                    self.assertEqual(report['activation_lock_observer']['status'], 'STOPPED')
                    for key in ('activation_committed', 'disable_committed', 'exact_ordered_read_equal',
                                'update_trigger_rollback_equal', 'disabled_fallback_equal',
                                'source_count_min_max_unchanged'):
                        self.assertIs(report[key], True, key)
            finally:
                server.cleanup()
