#!/usr/bin/env python3
"""Replay a yield diagnostic JSON in memory; never connect to an operational DB.

The input schema is the read-only export with devices[ip].state/snapshots/profiles.
Catalog resolution uses the current checkout/environment, not an archived server
catalog. A 400-row export cannot reconstruct older observations absent from it.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import connect_sqlite
from core.yield_engine import (
    _interval_operational_fields, _load_interval_snapshots,
    estimate_cartridge_interval,
)

SNAPSHOT_COLUMNS = (
    "id", "printer_ip", "color", "usage_counter", "toner_level", "raw_level",
    "source", "valid", "reject_reason", "timestamp", "cartridge_key",
    "full_color", "print_total",
)


def replay_export(data: dict) -> dict:
    """Return decisions using only exported data and an explicit in-memory adapter."""
    conn = connect_sqlite(':memory:')
    try:
        conn.execute("""CREATE TABLE toner_snapshots_v2 (
            id INTEGER PRIMARY KEY, printer_ip TEXT, color TEXT,
            usage_counter INTEGER, toner_level INTEGER, raw_level INTEGER,
            source TEXT, valid INTEGER, reject_reason TEXT, timestamp TEXT,
            cartridge_key TEXT, full_color INTEGER, print_total INTEGER)""")
        for ip, device in data['devices'].items():
            states = device.get('state') or []
            if len(states) != 1:
                raise ValueError('Expected exactly one exported state per device')
            color = states[0].get('color') or 'black'
            for row in device.get('snapshots', []):
                row = dict(row, printer_ip=ip, color=color)
                conn.execute(
                    'INSERT INTO toner_snapshots_v2 (' + ','.join(SNAPSHOT_COLUMNS)
                    + ') VALUES (' + ','.join('?' for _ in SNAPSHOT_COLUMNS) + ')',
                    tuple(row.get(k) for k in SNAPSHOT_COLUMNS),
                )
        result = {'scope': 'exported snapshots only; current catalog resolution', 'devices': {}}
        for ip, device in data['devices'].items():
            state = dict(device['state'][0], printer_ip=ip)
            color = state.get('color') or 'black'
            profiles = device.get('profiles') or []
            profile = next((p for p in profiles if p.get('cartridge_key') == state.get('cartridge_key')), None)
            rows = _load_interval_snapshots(conn, ip=ip, color=color)
            decision = estimate_cartridge_interval(conn, ip=ip, color=color, state=state,
                                                   profile=profile, snap_rows=[r[2:] for r in rows])
            result['devices'][ip] = {
                'exported_snapshots': len(device.get('snapshots', [])),
                'retained_endpoints': len(rows),
                'decision': asdict(decision),
                'operational_fields': _interval_operational_fields(state, decision, v2_active=True),
            }
        return result
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = replay_export(json.loads(args.input.read_text(encoding='utf-8-sig')))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + '\n', encoding='utf-8')
    else:
        print(text)


if __name__ == '__main__':
    main()
