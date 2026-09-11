from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager

import local_state
import piece_records


class _Logger:
    def warn(self, _message: str) -> None:
        pass


class _Config:
    logger = _Logger()


def _wait_for(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_value_stats_returns_stale_snapshot_while_refreshing(monkeypatch) -> None:
    stale = {"currency": "USD", "all_time": {"pieces": 1}}
    fresh = {"currency": "USD", "all_time": {"pieces": 2}}
    started = threading.Event()

    def compute(_gc):
        started.set()
        return (2, 2), fresh

    monkeypatch.setattr(piece_records, "_computeValueStats", compute)
    monkeypatch.setattr(
        piece_records,
        "_value_stats_memo",
        (time.time() - 1.0, (1, 1), stale),
    )
    monkeypatch.setattr(piece_records, "_value_stats_refreshing", False)

    assert piece_records.getValueStats(_Config()) is stale
    assert started.wait(1.0)
    assert _wait_for(lambda: piece_records._value_stats_memo[2] is fresh)
    assert piece_records._value_stats_refreshing is False


def test_aggregates_return_stale_snapshot_while_refreshing(monkeypatch) -> None:
    stale = {"per_day": [{"date": "2026-08-29", "count": 1}]}
    fresh = {"per_day": [{"date": "2026-08-30", "count": 2}]}
    started = threading.Event()

    def compute(_gc, *, days):
        assert days == 365
        started.set()
        return (2, 2), fresh

    monkeypatch.setattr(piece_records, "_computeAggregates", compute)
    monkeypatch.setattr(
        piece_records,
        "_aggregates_memo",
        {365: (time.time() - 1.0, (1, 1), stale)},
    )
    monkeypatch.setattr(piece_records, "_aggregates_refreshing", set())

    assert piece_records.getAggregates(_Config(), days=365) is stale
    assert started.wait(1.0)
    assert _wait_for(lambda: piece_records._aggregates_memo[365][2] is fresh)
    assert 365 not in piece_records._aggregates_refreshing


def test_cached_part_prices_use_chunked_batch_queries(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE hive_part_metadata_cache ("
        "part_num TEXT NOT NULL, color_id TEXT NOT NULL, moving_avg_price REAL, "
        "cached_at REAL NOT NULL, PRIMARY KEY(part_num, color_id))"
    )
    rows = [(f"part-{i}", str(i), float(i), 1000.0 + i) for i in range(401)]
    conn.executemany(
        "INSERT INTO hive_part_metadata_cache "
        "(part_num, color_id, moving_avg_price, cached_at) VALUES (?, ?, ?, ?)",
        rows,
    )
    selects: list[str] = []
    conn.set_trace_callback(lambda statement: selects.append(statement) if statement.startswith("SELECT") else None)

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(local_state, "initialize_local_state", lambda: None)
    monkeypatch.setattr(local_state, "_connection", connection)
    pairs = [(f"part-{i}", i) for i in range(401)]
    pairs.extend([("part-0", 0), (None, None)])

    result = local_state.get_cached_part_prices(pairs)

    assert len(result) == 401
    assert result[("part-0", "0")] == (0.0, 1000.0)
    assert result[("part-400", "400")] == (400.0, 1400.0)
    assert len(selects) == 2
    conn.close()
