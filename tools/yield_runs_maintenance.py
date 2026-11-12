"""Explicit yield maintenance CLI, never invoked by start/setup or app startup.

Run only in an approved offline window, after backing up and stopping writers.
Requires --approve-maintenance and reader OFF. Never prints a DSN or raw error.
"""
import argparse
import os


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('enable', 'disable'))
    parser.add_argument('--approve-maintenance', action='store_true')
    parser.add_argument('--timeout-seconds', type=int, default=600)
    args = parser.parse_args(argv)
    if not args.approve_maintenance:
        parser.error('--approve-maintenance is required; this takes an exclusive source-table lock')
    if not 1 <= args.timeout_seconds <= 600:
        parser.error('--timeout-seconds must be between 1 and 600')
    # Config loads .env when core.db is imported. Check the effective flag AFTER
    # import but approval BEFORE import, and before opening any DB connection.
    from core.db import db_connection
    from core.yield_run_maintenance import enable_yield_runs, disable_yield_runs
    if os.getenv('YIELD_DERIVED_RUNS_ENABLED', '0').strip().lower() in {'1', 'true', 'yes', 'on'}:
        parser.error('Turn YIELD_DERIVED_RUNS_ENABLED OFF before offline maintenance')
    operation = enable_yield_runs if args.action == 'enable' else disable_yield_runs
    try:
        with db_connection(commit=True) as conn:
            operation(conn, approved=True, timeout_seconds=args.timeout_seconds)
    except Exception as exc:
        state = getattr(exc, 'sqlstate', None)
        safe_state = state if isinstance(state, str) and len(state) == 5 and state.isalnum() else 'unknown'
        print('YIELD_MAINTENANCE_FAILED; transaction not reported successful; sqlstate='+safe_state)
        return 1
    print('YIELD_MAINTENANCE_'+args.action.upper()+'_COMMITTED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
