"""v0.3.0 preservation, coherent occupancy reads and caller-owned history."""

from contextlib import contextmanager
import sqlite3

import pytest

import local_state
import piece_records
import smart_bins_storage as storage
from smart_bins_test_support import close_keeper
from test_irl_import_boundary import GUARD, run_isolated


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    close_keeper()
    path = tmp_path / "state.sqlite"
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(path))
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(tmp_path / "machine.toml"))
    monkeypatch.setattr(piece_records, "_initialized", False)
    local_state.initialize_local_state()
    yield path
    close_keeper()


def test_all_dormant_imports_have_no_database_network_or_runtime_side_effects(tmp_path):
    run_isolated(GUARD + '''
import importlib
class RuntimeGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.startswith(('project_harvest_', 'subsystems.', 'smart_bins_physical_',
                                 'smart_bins_native_completion')) or
            (fullname.startswith('server.') and fullname != 'server.config_helpers') or
            fullname in ('coordinator', 'piece_transport', 'main')):
            raise AssertionError('Forbidden runtime import: ' + fullname)
sys.meta_path.insert(0, RuntimeGuard())
modules = ['local_state', 'piece_records', 'smart_bins_storage', 'smart_bins_eligibility',
           'smart_bins_migration', 'smart_bins_service', 'smart_bins_delivery',
           'smart_bins_completion_recovery', 'smart_bins_reconciliation',
           'smart_bins_followup_reconciliation', 'smart_bins_harvest_integration',
           'harvest_integration_storage']
for name in modules:
    importlib.import_module(name)
assert not any(n.split('.')[0] in ('hardware', 'machine_platform') or
               n.startswith('project_harvest_') for n in sys.modules)
assert 'irl.config' not in sys.modules
assert not sys.modules['piece_records']._initialized
''', tmp_path)


def test_ordinary_v030_pragmas_schema_keeper_and_resolver_remain(legacy_db):
    import machine_toml
    assert local_state._legacy_state_dir() == machine_toml.machine_toml_path().parent
    assert local_state._SCHEMA_VERSION == 5
    keeper = local_state._keeper_conn
    assert keeper is not None
    local_state._ensure_keeper_connection()
    assert local_state._keeper_conn is keeper
    with local_state._connection() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"power_stress_runs", "power_stress_events", "bin_state_current"} <= tables
        assert not any(name.startswith("smart_bin_") for name in tables)


def seed_occupancy():
    local_state.set_machine_id("m")
    session = local_state.start_new_sorting_session(reason="synthetic")
    local_state.record_piece_distribution({"uuid": "piece", "destination_bin": [0, 0, 0], "distributed_at": 123.0,
                                           "part_id": "3001", "color_id": "2", "category_id": "A"})
    return session["id"]


def test_occupancy_preserves_aggregate_only_and_inconsistent_evidence(legacy_db):
    session = seed_occupancy()
    with local_state._connection() as conn:
        conn.execute("INSERT INTO bin_item_aggregates "
                     "(session_id,layer_index,section_index,bin_index,item_key,category_id,count) "
                     "VALUES(?,0,0,1,'historical','B',4)", (session,))
        conn.execute("UPDATE bin_state_current SET piece_count=0 WHERE session_id=?", (session,))
        conn.commit()
    result = local_state.get_current_bin_occupancy_evidence()
    assert result[(0, 0, 0)] == (0, {"A": 1})
    assert result[(0, 0, 1)] == (0, {"B": 4})
    with local_state._connection() as conn:
        assert conn.execute("SELECT count(*) FROM piece_events").fetchone()[0] == 1
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'smart_bin_%'").fetchall()


def test_occupancy_counts_and_categories_share_one_read_snapshot(legacy_db, monkeypatch):
    seed_occupancy()
    original = local_state._connection
    injected = []

    class ConcurrentWrite:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, params=()):
            if "SUM(count) AS quantity" in sql:
                assert self.conn.in_transaction
                with original() as writer:
                    writer.execute("UPDATE bin_state_current SET piece_count=2")
                    writer.execute("UPDATE bin_item_aggregates SET count=2")
                    writer.commit()
                injected.append(True)
            return self.conn.execute(sql, params)

    @contextmanager
    def connection():
        with original() as conn:
            yield ConcurrentWrite(conn)

    monkeypatch.setattr(local_state, "_connection", connection)
    assert local_state.get_current_bin_occupancy_evidence()[(0, 0, 0)] == (1, {"A": 1})
    assert injected == [True]
    with original() as conn:
        assert conn.execute("SELECT piece_count FROM bin_state_current").fetchone()[0] == 2


@pytest.mark.parametrize("commit", [False, True])
def test_history_never_connects_or_commits_and_shares_outer_outcome(legacy_db, monkeypatch, commit):
    storage.initialize_schema()
    piece_records.initialize_piece_records()
    conn = local_state._connect()
    try:
        with storage.critical_transaction(conn) as tx:
            tx.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('atomic')")
            def forbidden(*args, **kwargs):
                raise AssertionError("caller-owned helper must not connect")
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3, "connect", forbidden)
                piece_records.recordPieceOnConnection(tx, {
                    "uuid": "atomic", "part_id": "3001", "destination_bin": [1, 2, 3],
                    "brickognize_listing_id": "listing", "color_provider": "provider", "color_confidence": 0.9,
                }, run_id="runtime", machine_id="m")
            assert tx.in_transaction
            with local_state._connection() as reader:
                assert reader.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 0
                assert reader.execute("SELECT count(*) FROM smart_bin_machines").fetchone()[0] == 0
            if commit:
                tx.commit()
            else:
                tx.rollback()
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        with local_state._connection() as reader:
            assert reader.execute("SELECT count(*) FROM piece_records").fetchone()[0] == int(commit)
            assert reader.execute("SELECT count(*) FROM smart_bin_machines").fetchone()[0] == int(commit)
    finally:
        conn.close()
