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
        root = Path(__file__).resolve().parents[1]
        expected = subprocess.check_output(['git', 'archive', '--format=zip', trial.HEAD], cwd=root)
        with zipfile.ZipFile(io.BytesIO(expected)) as archive:
            raw = subprocess.check_output(['git', 'show', trial.HEAD + ':cartridge_test.bat'], cwd=root)
            self.assertNotEqual(raw, archive.read('cartridge_test.bat'))
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
                    trial.execute_trial(ConnectionAdapter(raw, backend='postgresql'), root,
                                        report, expected_rows=len(rows))
                    for key in ('activation_committed', 'disable_committed', 'exact_ordered_read_equal',
                                'update_trigger_rollback_equal', 'disabled_fallback_equal',
                                'source_count_min_max_unchanged'):
                        self.assertIs(report[key], True, key)
            finally:
                server.cleanup()
