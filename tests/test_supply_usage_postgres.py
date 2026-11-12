"""Usage persistence through the real app adapter on disposable local PostgreSQL only."""
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from core import database, store
from core.enhanced_collector import _track_supply_usage
from core.supply_usage import usage_view
from tests.test_yield_run_prototype import private_server, psycopg, pgserver

pytestmark = [pytest.mark.pytest_only,
              pytest.mark.skipif(psycopg is None or pgserver is None,
                                 reason='optional local PostgreSQL dependencies not installed')]


def test_usage_survives_postgres_upsert_single_batch_and_cache_reload(private_server, monkeypatch):
    name = 'printguard_test_supply_' + uuid.uuid4().hex[:10]
    with psycopg.connect(private_server.get_uri(), autocommit=True) as admin:
        admin.execute('CREATE DATABASE ' + name)
    uri = private_server.get_uri(database=name)
    try:
        # Alembic configures logging globally; isolate it so subsequent tests retain their loggers.
        subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
                       cwd=Path(__file__).resolve().parents[1],
                       env={**os.environ, 'ALEMBIC_DB_URL': uri, 'DB_BACKEND': 'sqlite', 'PG_DSN': ''},
                       check=True, capture_output=True, text=True)
        monkeypatch.setenv('DB_BACKEND', 'postgresql')
        monkeypatch.setenv('PG_DSN', uri)
        # Own the cache too: no cross-test state or application DSN is used.
        monkeypatch.setattr(store, '_prev', store.PrevStore())
        ip = '192.0.2.83'
        toners = {color: {} for color in ('black', 'cyan', 'magenta', 'yellow')}
        store._prev.set(ip, {'print_total': 1000, 'cartridge_ids': {'black': 'CHIP-A'},
                             'cart_supply_pages': {'black': 7}})
        for total, fc in ((1000, 200), (1050, 220)):
            _track_supply_usage(ip, {}, toners, {'total': total, 'full_color': fc},
                                {'black': 'CHIP-A'}, {'black': 'chip_id'})
        single = database.load_printer_counters(ip)
        assert single is not None  # APIs log/swallow DB failures; assert an actual round trip.
        assert single['cartridge_ids'] == {'black': 'CHIP-A'}
        assert single['cart_supply_pages'] == {'black': 7}
        assert usage_view(single['supply_usage']['black'])['pages'] == 50
        for color in ('cyan', 'magenta', 'yellow'):
            assert usage_view(single['supply_usage'][color])['pages'] == 20
        assert database.load_printer_counters_many([ip, 'missing'])[ip] == single
        store._prev._cache.clear()
        assert store._prev.get(ip)['supply_usage'] == single['supply_usage']
        _track_supply_usage(ip, {}, toners, {'total': 1100, 'full_color': 230},
                            {'black': 'CHIP-B'}, {'black': 'chip_id'})
        replaced = database.load_printer_counters(ip)['supply_usage']
        assert usage_view(replaced['black'])['pages'] == 0
        assert replaced['black']['cartridge_id'] == 'CHIP-B'
        assert usage_view(replaced['cyan'])['pages'] == 30
    finally:
        with psycopg.connect(private_server.get_uri(), autocommit=True) as admin:
            admin.execute('DROP DATABASE ' + name)
