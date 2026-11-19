"""Pure guards for the approved, owned Windows restore copy. No DB/network I/O."""
import os
import unittest
from unittest.mock import Mock, patch

from tools import restore_yield_activation_copy as lab


class CopyRestoreGuards(unittest.TestCase):
    def plan(self):
        return dict(purpose='isolated-yield-activation-trial-only', source_commit=lab.HEAD,
                    dump_sha256=lab.DIGEST, lab_port=lab.PORT,
                    production_activation_authorized=False, backup_folder=str(lab.BACKUP))

    def test_valid_copy_plan(self):
        lab.check_plan(self.plan())

    def test_changed_or_production_plan_refused(self):
        for key, value in [('purpose', 'production'), ('source_commit', 'bad'),
                           ('dump_sha256', 'bad'), ('lab_port', 5432),
                           ('production_activation_authorized', True), ('backup_folder', 'other')]:
            with self.subTest(key=key):
                plan = self.plan()
                plan[key] = value
                with self.assertRaisesRegex(RuntimeError, 'PLAN_MISMATCH'):
                    lab.check_plan(plan)

    def test_production_libpq_settings_not_inherited_by_tools(self):
        with patch.dict(os.environ, {'PGHOSTADDR': 'bad', 'PGSERVICE': 'bad',
                                    'PGDATA': 'production', 'PGPASSWORD': 'private', 'PGPORT': '5432'}):
            env = lab.child_environment('lab-only')
        for name in ('PGHOSTADDR', 'PGSERVICE', 'PGDATA'):
            self.assertNotIn(name, env)
        self.assertEqual(env['PGPORT'], '55439')
        self.assertEqual(env['PGPASSWORD'], 'lab-only')
        self.assertNotIn('private', str(env))

    def test_owned_identity_accepted(self):
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = (
            '127.0.0.1', lab.PORT, lab.DB, lab.USER, str(lab.ROOT / 'data'), '123')
        self.assertEqual(lab.verify_identity(conn, lab.ROOT / 'data'), '123')

    def test_identity_query_uses_host_not_inet_text_cast(self):
        # PostgreSQL inet::text includes /32 even for a single IPv4 host.
        # Model both SQL forms so the original false rejection regresses here.
        conn = Mock()
        def execute(query):
            address = '127.0.0.1' if 'host(inet_server_addr())' in query else '127.0.0.1/32'
            cursor = Mock()
            cursor.fetchone.return_value = (
                address, lab.PORT, lab.DB, lab.USER, str(lab.ROOT / 'data'), '123')
            return cursor
        conn.execute.side_effect = execute
        self.assertEqual(lab.verify_identity(conn, lab.ROOT / 'data'), '123')

    def test_foreign_identity_refused(self):
        valid = ['127.0.0.1', lab.PORT, lab.DB, lab.USER, str(lab.ROOT / 'data'), '123']
        for index, value in enumerate(['192.0.2.1', 5432, 'production', 'wrong_user', 'other-directory']):
            with self.subTest(field=index):
                row = valid.copy()
                row[index] = value
                conn = Mock()
                conn.execute.return_value.fetchone.return_value = tuple(row)
                with self.assertRaisesRegex(RuntimeError, 'LAB_IDENTITY_MISMATCH'):
                    lab.verify_identity(conn, lab.ROOT / 'data')

    def test_wrong_os_refused_before_any_subprocess(self):
        with patch.object(lab.os, 'name', 'posix'), patch.object(lab.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'WINDOWS_ONLY'):
                lab.main()
            run.assert_not_called()
