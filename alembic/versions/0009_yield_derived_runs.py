"""Create lightweight, initially unready yield metadata; no source-table DDL.

Revision ID: 0009
Revises: 0008

Source index, writer triggers and initial build belong to explicit approved
maintenance, NOT Alembic upgrade/startup. SQLite is intentionally a no-op.
"""
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_context().dialect.name != 'postgresql':
        return
    # No lock/index/trigger on the source table during routine upgrade.
    op.execute("SET LOCAL lock_timeout = '5s'")
    script = (Path(__file__).resolve().parents[1] / 'sql/0009_yield_runs.sql').read_text(encoding='utf-8')
    context = op.get_context()
    # Offline generation follows Alembic's default schema; online respects the
    # connection's current app schema (including isolated test search_paths).
    schema = 'public' if context.as_sql else op.get_bind().scalar(sa.text('SELECT current_schema()'))
    script = script.replace('__APP_SCHEMA__', context.dialect.identifier_preparer.quote(schema))
    for statement in script.split('-- yield-runs-statement')[1:]:
        op.execute(sa.text(statement))


def downgrade():
    if op.get_context().dialect.name != 'postgresql':
        return
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('LOCK TABLE toner_snapshots_v2 IN ACCESS EXCLUSIVE MODE')
    for trigger in ('lock', 'insert', 'delete', 'update', 'truncate'):
        op.execute(f'DROP TRIGGER IF EXISTS yield_runs_{trigger} ON toner_snapshots_v2')
    op.execute('DROP INDEX IF EXISTS yield_runs_snapshot_neighbor')
    # Explicit dependency order: never CASCADE into source/application objects.
    for function in ('clear_runs()', 'maintain()', 'repair_one(bigint)', 'lock_slots()'):
        op.execute(f'DROP FUNCTION IF EXISTS yield_run_{function}')
    for table in ('boundaries', 'slots', 'meta'):
        op.execute(f'DROP TABLE yield_run_{table}')
