"""Explicit-maintenance boundary and fail-closed PowerShell launcher contracts."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

from core.yield_run_maintenance import enable_yield_runs, disable_yield_runs
from tools.yield_runs_maintenance import main

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('pwsh') or shutil.which('powershell')


class MaintenanceApprovalTests(unittest.TestCase):
    def test_library_requires_approval_before_any_sql(self):
        for action in (enable_yield_runs, disable_yield_runs):
            conn = Mock()
            with self.assertRaisesRegex(ValueError, 'APPROVAL_REQUIRED'):
                action(conn)
            conn.execute.assert_not_called()

    def test_cli_requires_approval_before_loading_database_config(self):
        import builtins
        original = builtins.__import__
        def guard(name, *args, **kwargs):
            if name.startswith('core'):
                raise AssertionError('CLI touched DB config without approval')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=guard), patch('sys.stderr'):
            with self.assertRaises(SystemExit) as error:
                main(['enable'])
        self.assertEqual(error.exception.code, 2)

    def test_cli_refuses_reader_on_without_opening_connection(self):
        with patch('core.db.db_connection') as connect, patch.dict('os.environ', {'YIELD_DERIVED_RUNS_ENABLED': '1'}), patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                main(['disable', '--approve-maintenance'])
            connect.assert_not_called()

    def test_cli_nonzero_and_safe_message_after_transaction_failure(self):
        from contextlib import contextmanager
        events = []
        @contextmanager
        def connection(**kwargs):
            try:
                yield Mock()
                events.append('commit')
            except Exception:
                events.append('rollback')
                raise
        with patch.dict('os.environ', {'YIELD_DERIVED_RUNS_ENABLED': '0'}), \
             patch('core.db.db_connection', connection), \
             patch('core.yield_run_maintenance.enable_yield_runs', side_effect=RuntimeError('private DSN')), \
             patch('builtins.print') as output:
            self.assertEqual(main(['enable', '--approve-maintenance']), 1)
        self.assertEqual(events, ['rollback'])
        self.assertNotIn('private DSN', str(output.call_args))

    def test_migration_upgrade_contains_no_source_ddl_or_activation(self):
        path = ROOT/'alembic/versions/0009_yield_derived_runs.py'
        spec = importlib.util.spec_from_file_location('guard_0009', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        op = Mock()
        op.get_context.return_value.dialect.name = 'postgresql'
        op.get_context.return_value.as_sql = False
        op.get_context.return_value.dialect.identifier_preparer.quote.return_value = 'public'
        op.get_bind.return_value.scalar.return_value = 'public'
        with patch.object(module, 'op', op):
            module.upgrade()
        statements = '\n'.join(str(call.args[0]) for call in op.execute.call_args_list)
        self.assertIn('CREATE TABLE public.yield_run_meta', statements)
        self.assertNotIn('toner_snapshots_v2', statements)
        self.assertNotIn('CREATE TRIGGER', statements)
        self.assertNotIn('CREATE FUNCTION', statements)
        self.assertNotIn('ACCESS EXCLUSIVE', statements)

    def test_launchers_never_invoke_activation_or_stamp_failed_migrations(self):
        for name in ('start.ps1', 'setup.ps1'):
            text = (ROOT/name).read_text(encoding='utf-8-sig')
            self.assertNotIn("'stamp', 'head'", text)
            self.assertNotIn('yield_runs_maintenance', text)
            self.assertNotIn('enable_yield_runs', text)
            self.assertIn('$migrationCode', text)
            self.assertTrue((ROOT/name).read_bytes().startswith(b'\xef\xbb\xbf'))


@unittest.skipUnless(POWERSHELL, 'PowerShell runtime is exercised by Windows CI')
class LauncherFailureExecutionTests(unittest.TestCase):
    """Execute the REAL DB block only, with fake Python commands; no DB/server I/O."""
    def run_block(self, name, migration_code, other_code=0):
        text = (ROOT/name).read_text(encoding='utf-8-sig')
        if name == 'start.ps1':
            a = text.index('    $env:ALEMBIC_DB_URL = $pgDsn')
            b = text.index('} else {', a)
            block = text[a:b]
        else:
            a = text.index('# Never stamp a failed migration')
            b = text.index('function Get-TargetDbState', a)
            block = text[a:b]
        harness = f"""
$pgDsn='unused-test-dsn'
$PyCmd='FakePython'
function Invoke-Quiet {{ return {migration_code} }}
function FakePython {{ $global:LASTEXITCODE={other_code}; Write-Output '0009 (head)'; Write-Host 'PYTHON_AFTER_MIGRATION' }}
function Pop-Location {{}}
function Add-SetupError {{ Write-Host 'SETUP_ERROR' }}
function Add-LogToError {{}}
function Exit-Fatal {{ exit 1 }}
""" + block + "\nWrite-Host 'WEB_START_ALLOWED'\nexit 0\n"
        # EncodedCommand is UTF-16LE on both Windows PowerShell and pwsh; avoid
        # locale stdin decoding and quoting differences in the CI subprocess.
        import base64
        encoded = base64.b64encode(harness.encode('utf-16le')).decode('ascii')
        return subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)

    def test_start_stops_before_auto_import_and_web_on_migration_failure(self):
        result = self.run_block('start.ps1', 1)
        self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
        self.assertNotIn('PYTHON_AFTER_MIGRATION', result.stdout)
        self.assertNotIn('WEB_START_ALLOWED', result.stdout)

    def test_start_stops_on_auto_import_failure(self):
        result = self.run_block('start.ps1', 0, 1)
        self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
        self.assertNotIn('WEB_START_ALLOWED', result.stdout)

    def test_setup_stops_on_migration_failure_without_stamp_or_retry(self):
        result = self.run_block('setup.ps1', 1)
        self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
        self.assertNotIn('PYTHON_AFTER_MIGRATION', result.stdout)
        self.assertNotIn('WEB_START_ALLOWED', result.stdout)

    def test_setup_stops_when_current_revision_command_fails(self):
        result = self.run_block('setup.ps1', 0, 1)
        self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
        self.assertNotIn('WEB_START_ALLOWED', result.stdout)

    def test_successful_db_blocks_allow_continuation(self):
        for name in ('start.ps1', 'setup.ps1'):
            result = self.run_block(name, 0)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('WEB_START_ALLOWED', result.stdout)
