"""Windows COPY-only lifecycle for the existing read-only history diagnostic.

No application imports, .env loading, restore, ANALYZE, activation or production DSN.
Native children write directly to files, never to PowerShell-managed pipes. Progress
is flushed every 30 seconds. A hard 600-second child budget supplements SQL deadlines.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

LAB = Path(r'E:\PrintGuard-Test\yield-activation-20261119-085347-ad6696-v2')
BIN = Path(r'E:\pgsql\binaries\bin')
SOURCE_COMMIT = 'cbcac7e4ad9a2021bdc8683aebd19547b02d450c'
SYSTEM_ID = '7709766716166001212'
SOURCE_FILES = ('tools/diagnose_yield_history.py', 'tools/yield_query_lab.py', 'core/yield_engine.py')


class CopyFailure(RuntimeError):
    """Only fixed codes, never driver text or private log content."""


def emit(message):
    print(message, flush=True)


def child_environment():
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'COMSPEC', 'SYSTEMDRIVE'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHON_DOTENV_DISABLED='1', YIELD_DERIVED_RUNS_ENABLED='0')
    return env


def run_command(argv, log, *, env, cwd, phase, budget, expected=(0,), stderr_log=None):
    """Direct file handles avoid inherited PowerShell pipe lifetimes; no raw output."""
    emit('PHASE=' + phase)
    started = time.monotonic()
    process = None
    with ExitStack() as handles:
        output = handles.enter_context(log.open('xb'))
        errors = handles.enter_context(stderr_log.open('xb')) if stderr_log else subprocess.STDOUT
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                stderr=errors, env=env, cwd=cwd,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0))
            while True:
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0:
                    raise CopyFailure('CHILD_BUDGET_EXCEEDED')
                try:
                    code = process.wait(timeout=min(30, remaining))
                    break
                except subprocess.TimeoutExpired:
                    emit('WAIT phase=' + phase + ' elapsed_seconds=' + str(int(time.monotonic() - started)))
            if code not in expected:
                raise CopyFailure('CHILD_EXIT_NONZERO')
            emit('PHASE_DONE=' + phase)
            return code
        finally:
            # Only this owned child is terminated, never a PID discovered by name.
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def preflight(source, env):
    if os.name != 'nt':
        raise CopyFailure('WINDOWS_ONLY')
    for path in (BIN / 'pg_ctl.exe', BIN / 'pg_controldata.exe', LAB / 'data/PG_VERSION',
                 LAB / 'lab-auth.json', *(source / p for p in SOURCE_FILES)):
        if not path.is_file():
            raise CopyFailure('REQUIRED_FILE_MISSING')
    head = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
        capture_output=True, timeout=10, env=env)
    if head.returncode or head.stdout.decode('ascii', errors='replace').strip() != SOURCE_COMMIT:
        raise CopyFailure('SOURCE_COMMIT_MISMATCH')
    diff = subprocess.run(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--', *SOURCE_FILES],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, env=env)
    if diff.returncode:
        raise CopyFailure('SOURCE_FILES_CHANGED')
    previous = json.loads((LAB / 'activation-trial-lock-observed-report.json').read_text(encoding='utf-8-sig'))
    required = ('disable_committed', 'disabled_fallback_equal', 'source_count_min_max_unchanged', 'cluster_stopped')
    if previous.get('status') != 'COPY_HISTORY_TRIAL_PASSED' or not all(previous.get(k) is True for k in required):
        raise CopyFailure('PREVIOUS_COPY_CLEANUP_UNVERIFIED')
    auth = json.loads((LAB / 'lab-auth.json').read_text(encoding='utf-8-sig'))
    if set(auth) != {'host', 'port', 'user', 'dbname', 'password'} or (
            auth.get('host'), auth.get('port'), auth.get('user'), auth.get('dbname')) != (
            '127.0.0.1', 55439, 'yield_activation_lab', 'yield_activation_copy'):
        raise CopyFailure('COPY_AUTH_TARGET_MISMATCH')
    if not isinstance(auth['password'], str) or not auth['password']:
        raise CopyFailure('COPY_PASSWORD_MISSING')
    if (LAB / 'data/postmaster.pid').exists():
        raise CopyFailure('COPY_ALREADY_RUNNING_OR_PID_FILE_PRESENT')
    status = subprocess.run([str(BIN / 'pg_ctl.exe'), '-D', str(LAB / 'data'), 'status'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=10, env=env)
    if status.returncode != 3:
        raise CopyFailure('COPY_STOPPED_STATE_UNVERIFIED')
    # Offline identifier check before starting anything. C locale is for message labels only.
    import re
    control = subprocess.run([str(BIN / 'pg_controldata.exe'), '-D', str(LAB / 'data')],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10, env={**env, 'LC_ALL': 'C'})
    identifier = re.search(rb'Database system identifier:\s*(\d+)', control.stdout)
    if control.returncode or not identifier or identifier.group(1).decode('ascii') != SYSTEM_ID:
        raise CopyFailure('OFFLINE_COPY_IDENTITY_MISMATCH')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 55439))
    emit('PREFLIGHT_PASSED=True')


def execute(source, folder, env):
    data = LAB / 'data'
    ctl = [str(BIN / 'pg_ctl.exe'), '-D', str(data)]
    report = {'status': 'FAILED', 'copy_stopped': False, 'phase': 'COPY_START'}
    try:
        run_command(ctl + ['-l', str(folder / 'postgresql.log'), '-o',
            '-h 127.0.0.1 -p 55439 -c max_connections=12 -c default_transaction_read_only=on',
            '-w', '-t', '60', 'start'], folder / 'start.log', env=env, cwd=LAB,
            phase='COPY_START', budget=90)
        report['phase'] = 'READONLY_COMPARISON'
        run_command([sys.executable, '-I', str(source / SOURCE_FILES[0]),
            '--postgres-copy-auth', str(LAB / 'lab-auth.json'),
            '--expected-system-id', SYSTEM_ID, '--expected-data-directory', str(data),
            '--timeout-seconds', '120', '--repeats', '2', '--explain'],
            folder / 'comparison-output.log', env=env, cwd=source,
            phase='READONLY_COMPARISON', budget=600, stderr_log=folder / 'diagnostic-stderr.log')
        result = json.loads((folder / 'comparison-output.log').read_text(encoding='utf-8-sig'))
        if result.get('status') != 'READONLY_COMPARISON_PASSED' or result.get('exact_ordered_equal') is not True:
            raise CopyFailure('COMPARISON_NOT_VERIFIED')
        if result.get('history_rows') != 2256043 or result.get('slots') != 51:
            raise CopyFailure('COPY_DATASET_MISMATCH')
        report['comparison'] = {k: result[k] for k in (
            'history_rows', 'slots', 'exact_ordered_equal', 'measurements', 'plans')}
        report['status'] = 'READONLY_COMPARISON_PASSED'
        report['phase'] = 'COMPLETE'
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        report['error_code'] = str(exc) if type(exc) is CopyFailure else 'UNCLASSIFIED'
    finally:
        # Preflight already proved stopped; this lifecycle owns only the pinned copy.
        try:
            code = run_command(ctl + ['status'], folder / 'cleanup-status-before.log',
                env=env, cwd=LAB, phase='COPY_STATUS_BEFORE_STOP', budget=10, expected=(0, 3))
            if code == 0:
                run_command(ctl + ['-m', 'fast', '-w', '-t', '60', 'stop'], folder / 'stop.log',
                    env=env, cwd=LAB, phase='COPY_STOP', budget=90)
            code = run_command(ctl + ['status'], folder / 'cleanup-status-after.log',
                env=env, cwd=LAB, phase='VERIFY_COPY_STOPPED', budget=10, expected=(3,))
            report['copy_stopped'] = code == 3 and not (data / 'postmaster.pid').exists()
        except BaseException:
            report['copy_stopped'] = False
        if not report['copy_stopped']:
            report['status'] = 'FAILED_CLEANUP_NOT_CONFIRMED'
        (folder / 'runner-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        emit('COPY_STOPPED=' + str(report['copy_stopped']))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        source = args.source_root.resolve(strict=True)
        env = child_environment()
        preflight(source, env)
        folder = LAB / ('join-review-python-' + uuid.uuid4().hex[:12])
        folder.mkdir(exist_ok=False)
        emit('REPORT_FOLDER=' + str(folder))
        report = execute(source, folder, env)
        emit(json.dumps(report, indent=2))
        return 0 if report['status'] == 'READONLY_COMPARISON_PASSED' and report['copy_stopped'] else 1
    except Exception as exc:
        emit('RUNNER_FAILED_TYPE=' + type(exc).__name__)
        emit('RUNNER_FAILED_CODE=' + (str(exc) if type(exc) is CopyFailure else 'UNCLASSIFIED'))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
