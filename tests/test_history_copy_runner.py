"""Lifecycle tests use owned children/mocks, never Windows or an operational DSN."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import run_history_copy_diagnostic as runner


class CommandTests(unittest.TestCase):
    def test_real_child_output_goes_to_file_not_console_or_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                runner.run_command([sys.executable, '-c', 'print("private payload")'],
                    folder / 'output.log', env=os.environ.copy(), cwd=folder,
                    phase='TEST', budget=5)
            self.assertIn('private payload', (folder / 'output.log').read_text())
            self.assertNotIn('private payload', output.getvalue())
            self.assertIn('PHASE_DONE=TEST', output.getvalue())

    def test_real_hanging_child_is_terminated_at_budget_and_heartbeat_is_flushed(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            output = io.StringIO()
            original = subprocess.Popen
            children = []
            def spawn(*args, **kwargs):
                child = original(*args, **kwargs)
                children.append(child)
                self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
                self.assertNotEqual(kwargs['stdout'], subprocess.PIPE)
                return child
            with patch.object(runner.subprocess, 'Popen', side_effect=spawn), contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(runner.CopyFailure, '^CHILD_BUDGET_EXCEEDED$'):
                    runner.run_command([sys.executable, '-c', 'import time; time.sleep(30)'],
                        folder / 'output.log', env=os.environ.copy(), cwd=folder,
                        phase='TEST', budget=0.15)
            self.assertIsNotNone(children[0].poll())
            self.assertIn('WAIT phase=TEST', output.getvalue())

    def test_stderr_does_not_corrupt_json_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                runner.run_command([sys.executable, '-c',
                    'import sys; print("{}"); print("private warning", file=sys.stderr)'],
                    folder/'result.json', stderr_log=folder/'stderr.log',
                    env=os.environ.copy(), cwd=folder, phase='TEST', budget=5)
            self.assertEqual(json.loads((folder/'result.json').read_text()), {})
            self.assertIn('private warning', (folder/'stderr.log').read_text())
            self.assertNotIn('private warning', output.getvalue())

    def test_secret_environment_is_not_inherited(self):
        with patch.dict(os.environ, {'PG_DSN': 'private', 'PGPASSWORD': 'private',
                                    'SECRET_KEY': 'private', 'PYTHONPATH': 'private'}):
            env = runner.child_environment()
        for key in ('PG_DSN', 'PGPASSWORD', 'SECRET_KEY', 'PYTHONPATH'):
            self.assertNotIn(key, env)
        self.assertEqual(env['YIELD_DERIVED_RUNS_ENABLED'], '0')


class LifecycleTests(unittest.TestCase):
    def exercise(self, fail=None, cleanup_failure=False, result_override=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        lab, source, folder = root/'lab', root/'source', root/'results'
        for path in (lab/'data', source, folder):
            path.mkdir(parents=True)
        calls = []
        result = {'status': 'READONLY_COMPARISON_PASSED', 'exact_ordered_equal': True,
                  'history_rows': 2256043, 'slots': 51, 'measurements': [], 'plans': {}}
        result.update(result_override or {})
        def command(argv, log, **kwargs):
            phase = kwargs['phase']
            calls.append((phase, argv, kwargs))
            self.assertEqual(argv[1:3], ['-D', str(lab/'data')]) if phase != 'READONLY_COMPARISON' else None
            if phase == fail or (cleanup_failure and phase == 'COPY_STOP'):
                raise RuntimeError('private password from child')
            if phase == 'READONLY_COMPARISON':
                log.write_text(json.dumps(result))
            return 3 if phase == 'VERIFY_COPY_STOPPED' else 0
        console = io.StringIO()
        with patch.object(runner, 'LAB', lab), patch.object(runner, 'BIN', root/'bin'), \
                patch.object(runner, 'run_command', side_effect=command), contextlib.redirect_stdout(console):
            report = runner.execute(source, folder, {})
        self.assertNotIn('private password', console.getvalue())
        self.assertNotIn('private password', (folder/'runner-report.json').read_text())
        return report, calls

    def test_success_starts_only_copy_compares_readonly_and_stops(self):
        report, calls = self.exercise()
        self.assertEqual(report['status'], 'READONLY_COMPARISON_PASSED')
        self.assertIs(report['copy_stopped'], True)
        self.assertEqual([p for p, _, _ in calls], ['COPY_START', 'READONLY_COMPARISON',
            'COPY_STATUS_BEFORE_STOP', 'COPY_STOP', 'VERIFY_COPY_STOPPED'])
        comparison = calls[1]
        self.assertIn('--expected-system-id', comparison[1])
        self.assertIn('--postgres-copy-auth', comparison[1])
        self.assertEqual(comparison[2]['budget'], 600)
        self.assertIn('default_transaction_read_only=on', ' '.join(calls[0][1]))
        for _, argv, _ in calls:
            text = ' '.join(argv)
            for forbidden in ('restore_yield', 'trial_yield', 'yield_runs_maintenance', 'ANALYZE'):
                self.assertNotIn(forbidden, text)

    def test_start_failure_still_checks_and_stops_copy(self):
        report, calls = self.exercise(fail='COPY_START')
        self.assertEqual(report['status'], 'FAILED')
        self.assertIs(report['copy_stopped'], True)
        self.assertIn('COPY_STOP', [p for p, _, _ in calls])
        self.assertNotIn('READONLY_COMPARISON', [p for p, _, _ in calls])

    def test_comparison_failure_still_stops_copy(self):
        report, _ = self.exercise(fail='READONLY_COMPARISON')
        self.assertIs(report['copy_stopped'], True)
        self.assertEqual(report['error_code'], 'UNCLASSIFIED')
        self.assertEqual(report['status'], 'FAILED')

    def test_shutdown_failure_cannot_be_reported_as_pass(self):
        report, _ = self.exercise(cleanup_failure=True)
        self.assertEqual(report['status'], 'FAILED_CLEANUP_NOT_CONFIRMED')
        self.assertIs(report['copy_stopped'], False)

    def test_unequal_output_cannot_be_reported_as_pass(self):
        report, _ = self.exercise(result_override={'exact_ordered_equal': False})
        self.assertEqual(report['error_code'], 'COMPARISON_NOT_VERIFIED')
        self.assertIs(report['copy_stopped'], True)

    def test_wrong_dataset_cannot_be_reported_as_pass(self):
        report, _ = self.exercise(result_override={'history_rows': 1})
        self.assertEqual(report['error_code'], 'COPY_DATASET_MISMATCH')


class PreflightTests(unittest.TestCase):
    def exercise(self, change=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lab, source, binary = root/'lab', root/'source', root/'bin'
            paths = [lab/'data/PG_VERSION', binary/'pg_ctl.exe', binary/'pg_controldata.exe']
            paths += [source/p for p in runner.SOURCE_FILES]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            prior = dict(status='COPY_HISTORY_TRIAL_PASSED', disable_committed=True,
                disabled_fallback_equal=True, source_count_min_max_unchanged=True, cluster_stopped=True)
            auth = dict(host='127.0.0.1', port=55439, user='yield_activation_lab',
                dbname='yield_activation_copy', password='private')
            if change == 'auth':
                auth['hostaddr'] = '192.0.2.1'
            if change == 'cleanup':
                prior['disable_committed'] = False
            (lab/'lab-auth.json').write_text(json.dumps(auth))
            (lab/'activation-trial-lock-observed-report.json').write_text(json.dumps(prior))
            head = b'wrong' if change == 'head' else runner.SOURCE_COMMIT.encode()
            identifier = b'wrong' if change == 'identity' else runner.SYSTEM_ID.encode()
            responses = [subprocess.CompletedProcess([], 0, head),
                         subprocess.CompletedProcess([], 1 if change == 'diff' else 0),
                         subprocess.CompletedProcess([], 0 if change == 'running' else 3),
                         subprocess.CompletedProcess([], 0, b'Database system identifier: '+identifier)]
            with patch.object(runner, 'LAB', lab), patch.object(runner, 'BIN', binary), \
                    patch.object(runner.os, 'name', 'nt'), \
                    patch.object(runner.subprocess, 'run', side_effect=responses), \
                    patch.object(runner.socket, 'socket'), contextlib.redirect_stdout(io.StringIO()):
                runner.preflight(source, {})

    def test_exact_source_owned_identity_and_cleanup_are_required(self):
        self.exercise()
        for change in ('auth', 'cleanup', 'head', 'identity', 'diff', 'running'):
            with self.subTest(change=change), self.assertRaises(runner.CopyFailure):
                self.exercise(change)


if __name__ == '__main__':
    unittest.main()
