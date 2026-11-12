"""Explicit offline yield activation/deactivation. NEVER called by startup/Alembic.

Caller owns commit/rollback. No automatic retry of this maintenance transaction.
"""
from pathlib import Path
import time

from core.db.connection import quote_ident
from core.yield_runs import rebuild_yield_runs

TRIGGERS = ('lock', 'insert', 'delete', 'update', 'truncate')
FUNCTIONS = ('clear_runs()', 'maintain()', 'repair_one(bigint)', 'lock_slots()')


def _prepare(conn, approved, timeout_seconds):
    # Approval and ownership checks precede ALL SQL.
    if approved is not True:
        raise ValueError('EXPLICIT_MAINTENANCE_APPROVAL_REQUIRED')
    if conn.backend != 'postgresql' or conn.raw_connection.autocommit:
        raise ValueError('POSTGRESQL_TRANSACTION_REQUIRED')
    if conn.raw_connection.info.transaction_status != 0:
        raise ValueError('IDLE_CALLER_OWNED_CONNECTION_REQUIRED')
    if not 1 <= timeout_seconds <= 600:
        raise ValueError('MAINTENANCE_BUDGET_MUST_BE_1_TO_600_SECONDS')
    deadline = time.monotonic() + timeout_seconds
    if conn.execute('SHOW transaction_isolation').fetchone()[0] != 'read committed':
        raise ValueError('READ_COMMITTED_MAINTENANCE_REQUIRED')
    conn.execute("SELECT set_config('lock_timeout', '1000ms', true)")
    def budget(*_):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('MAINTENANCE_BUDGET_EXCEEDED')
        conn.execute("SELECT set_config('statement_timeout', ?, true)",
                     (str(max(1, int(min(120, remaining) * 1000)))+'ms',))
    budget()
    present = conn.execute("""SELECT to_regclass('yield_run_meta') IS NOT NULL
        AND to_regclass('yield_run_slots') IS NOT NULL
        AND to_regclass('yield_run_boundaries') IS NOT NULL""").fetchone()[0]
    if not present:
        raise RuntimeError('RUN_ALEMBIC_UPGRADE_BEFORE_MAINTENANCE')
    conn.execute('LOCK TABLE toner_snapshots_v2 IN ACCESS EXCLUSIVE MODE')
    return budget


def enable_yield_runs(conn, *, approved=False, timeout_seconds=600, progress=None):
    """Install source index/triggers AND build complete summaries in ONE transaction.

    Repeated activation refuses existing objects instead of adopting/replacing
    possibly incompatible definitions. Disable explicitly before reactivation.
    Any exception requires caller rollback; CLI enforces that via db_connection.
    """
    budget = _prepare(conn, approved, timeout_seconds)
    existing = conn.execute("""SELECT to_regclass('yield_runs_snapshot_neighbor') IS NOT NULL
        OR EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='toner_snapshots_v2'::regclass
                   AND tgname IN ('yield_runs_lock', 'yield_runs_insert', 'yield_runs_delete',
                                  'yield_runs_update', 'yield_runs_truncate'))""").fetchone()[0]
    if existing:
        raise RuntimeError('ALREADY_INSTALLED_DISABLE_EXPLICITLY_BEFORE_REACTIVATION')
    schema = quote_ident(conn.execute('SELECT current_schema()').fetchone()[0])
    script = (Path(__file__).resolve().parents[1] / 'alembic/sql/0009_yield_runs_activation.sql').read_text(encoding='utf-8')
    for statement in script.replace('__APP_SCHEMA__', schema).split('-- yield-runs-statement')[1:]:
        budget()
        conn.execute(statement)
    def notify(phase, info):
        budget()
        if progress is not None:
            progress(phase, info)
    rebuild_yield_runs(conn, progress=notify, max_slots=256)
    budget()  # final caller commit still required; no ready state escapes on failure


def disable_yield_runs(conn, *, approved=False, timeout_seconds=600):
    """Remove writer overhead, invalidate summaries, preserve all source rows.

    Supports both the original unreleased 0009 layout and explicit activation.
    Lightweight Alembic tables/version remain; next startup cannot reinstall.
    """
    budget = _prepare(conn, approved, timeout_seconds)
    for trigger in TRIGGERS:
        budget()
        conn.execute(f'DROP TRIGGER IF EXISTS yield_runs_{trigger} ON toner_snapshots_v2')
    conn.execute('DROP INDEX IF EXISTS yield_runs_snapshot_neighbor')
    for function in FUNCTIONS:
        budget()
        conn.execute(f'DROP FUNCTION IF EXISTS yield_run_{function}')
    conn.execute('TRUNCATE yield_run_meta, yield_run_boundaries, yield_run_slots')
    budget()
