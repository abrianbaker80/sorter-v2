"""Retention releases SQLite before filesystem work and repairs interrupted sweeps."""

import os
import sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture(params=["channel_crops", "piece_images", "piece_link_images"])
def store(request, tmp_path, monkeypatch):
    # Redirect before imports as well as before the first connection.
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(tmp_path / "state.sqlite"))
    import channel_crop_store
    import piece_image_store

    table = request.param
    module = channel_crop_store if table == "channel_crops" else piece_image_store
    monkeypatch.setattr(module, "_initialized", False)
    cap = "_MAX_LINK_TOTAL_BYTES" if table == "piece_link_images" else "_MAX_TOTAL_BYTES"
    monkeypatch.setattr(module, cap, 0)
    monkeypatch.setattr(module, "_stats", dict(module._stats, evicted_files=0))
    logger = Mock()
    monkeypatch.setattr(module, "_logger", logger)
    module._ensureInitialized()
    base = getattr(module, table + "_dir")()
    sweep = module._linkRetentionSweep if table == "piece_link_images" else module._retentionSweep

    def seed(paths, *, times=None, synced=None, files=True):
        with module._connection() as conn:
            for i, path in enumerate(paths):
                if files:
                    target = base / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"x" * 10)
                values = {"created_at": times[i] if times else i + 1,
                          "bytes": 10, "file_path": path}
                if table != "channel_crops":
                    values.update(piece_uuid="retention-piece", seq=i)
                if table != "piece_link_images":
                    values["synced_at"] = synced[i] if synced else None
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(values)}) "
                    f"VALUES ({', '.join('?' for _ in values)})", tuple(values.values()),
                )
            conn.commit()

    def rows():
        with module._connection() as conn:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]

    yield SimpleNamespace(module=module, table=table, base=base, sweep=sweep,
                          cap=cap, seed=seed, rows=rows, logger=logger,
                          db=tmp_path / "state.sqlite")
    module._initialized = False


def test_writer_lock_is_free_at_every_unlink(store, monkeypatch):
    store.seed([f"parent/{i}.jpg" for i in range(4)])
    monkeypatch.setattr(store.module, store.cap, 20)
    original = Path.unlink
    probes = []
    listings = []
    original_iterdir = Path.iterdir

    def unlink(path, missing_ok=False):
        conn = sqlite3.connect(store.db, timeout=0)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
                probes.append(True)
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                probes.append(False)
        finally:
            conn.close()
        return original(path, missing_ok=missing_ok)

    def iterdir(path):
        listings.append(path)
        return original_iterdir(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unlink)
        patch.setattr(Path, "iterdir", iterdir)
        store.sweep()
    # Captured in the pre-fix report, including the first (read-only) unlink.
    print(f"{store.table}: write-lock probes={probes}; directory listings={len(listings)}")
    assert probes == [True, True]
    assert [r["deleted_at"] is not None for r in store.rows()] == [True, True, False, False]


def test_filesystem_has_no_connection_and_cleanup_is_bounded(store, monkeypatch):
    store.seed(["parent/a.jpg", "parent/b.jpg", "other/c.jpg", "root.jpg"])
    connection = store.module._connection
    active = []
    statements = []
    unlinks = []
    removals = []
    unlink = Path.unlink
    rmdir = Path.rmdir

    @contextmanager
    def tracked_connection():
        with connection() as conn:
            conn.set_trace_callback(statements.append)
            active.append(conn)
            try:
                yield conn
            finally:
                active.remove(conn)

    def checked_unlink(path, missing_ok=False):
        assert not active, "retention connection still open during unlink"
        unlinks.append(path)
        return unlink(path, missing_ok=missing_ok)

    def checked_rmdir(path):
        assert not active, "retention connection still open during rmdir"
        assert len(unlinks) == 4, "directory cleanup ran before all unlinks"
        removals.append(path)
        return rmdir(path)

    def forbidden(*args, **kwargs):
        pytest.fail("retention enumerated a directory")

    with monkeypatch.context() as patch:
        patch.setattr(store.module, "_connection", tracked_connection)
        patch.setattr(Path, "unlink", checked_unlink)
        patch.setattr(Path, "rmdir", checked_rmdir)
        for name in ("iterdir", "glob", "rglob"):
            patch.setattr(Path, name, forbidden)
        patch.setattr(os, "listdir", forbidden)
        patch.setattr(os, "scandir", forbidden)
        store.sweep()
    assert Counter(removals) == {store.base / "parent": 1, store.base / "other": 1}
    assert store.base.is_dir()
    assert sum(s.startswith("BEGIN") for s in statements) == 1
    assert statements.count("COMMIT") == 1
    assert sum(s.startswith(f"UPDATE {store.table}") for s in statements) == 4
    assert all(r["deleted_at"] is not None for r in store.rows())
    assert store.module._stats["evicted_files"] == 4
    store.logger.info.assert_called_once()
    assert "evicted 4 files" in store.logger.info.call_args.args[0]


def test_commit_failure_after_unlink_is_repaired_by_next_sweep(store, monkeypatch):
    store.seed(["parent/a.jpg", "parent/b.jpg"])
    before = store.rows()
    connection = store.module._connection
    failed_commits = []

    class FailingCommit:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def commit(self):
            assert self.conn.in_transaction
            assert not (store.base / "parent/a.jpg").exists()
            assert not (store.base / "parent/b.jpg").exists()
            failed_commits.append(True)
            raise sqlite3.OperationalError("injected tombstone commit failure")

    @contextmanager
    def fail_commit():
        with connection() as conn:
            yield FailingCommit(conn)

    with monkeypatch.context() as patch:
        patch.setattr(store.module, "_connection", fail_commit)
        with pytest.raises(sqlite3.OperationalError, match="injected tombstone"):
            store.sweep()
    assert failed_commits == [True]
    assert store.rows() == before  # Closing the failed connection rolled back.
    assert store.module._stats["evicted_files"] == 0
    store.logger.info.assert_not_called()
    missing = []
    unlink = Path.unlink

    def replay_unlink(path, missing_ok=False):
        missing.append(not path.exists())
        assert missing_ok
        return unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", replay_unlink)
        store.sweep()
    assert missing == [True, True]
    after = store.rows()
    assert all(r["deleted_at"] is not None for r in after)
    assert [{**r, "deleted_at": None} for r in after] == before
    assert store.module._stats["evicted_files"] == 2
    # A further replay neither recreates files nor counts the same rows again.
    store.sweep()
    assert store.rows() == after
    assert store.module._stats["evicted_files"] == 2


def test_victim_order_and_byte_cap_are_preserved(store, monkeypatch):
    store.seed([f"p/{i}.jpg" for i in range(4)],
               times=[10, 40, 30, 20], synced=[None, 1, 1, None])
    monkeypatch.setattr(store.module, store.cap, 20)
    before = store.rows()
    store.sweep()
    after = store.rows()
    expected = [False, True, True, False] if store.table != "piece_link_images" else [True, False, False, True]
    assert [r["deleted_at"] is not None for r in after] == expected
    assert sum(r["bytes"] for r in after if r["deleted_at"] is None) == 20
    assert [{**r, "deleted_at": None} for r in after] == before
    assert [(store.base / r["file_path"]).exists() for r in after] == [not v for v in expected]


def test_expected_filesystem_errors_do_not_abort_tombstones(store, monkeypatch):
    store.seed(["p/a.jpg", "p/b.jpg"])
    unlink = Path.unlink
    attempted = []

    def failing_unlink(path, missing_ok=False):
        if path.name == "a.jpg":
            raise PermissionError("injected file in use")
        return unlink(path, missing_ok=missing_ok)

    def failing_rmdir(path):
        attempted.append(path)
        raise OSError("injected nonempty directory")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", failing_unlink)
        patch.setattr(Path, "rmdir", failing_rmdir)
        store.sweep()
    assert attempted == [store.base / "p"]
    assert all(r["deleted_at"] is not None for r in store.rows())
    assert (store.base / "p/a.jpg").is_file()
    assert not (store.base / "p/b.jpg").exists()


def test_selection_limit_is_preserved(store):
    limit = 1000 if store.table == "channel_crops" else 500
    # Missing files avoid a large filesystem fixture while testing real SQL LIMIT.
    store.seed([f"p/{i}.jpg" for i in range(limit + 1)], files=False)
    store.sweep()
    assert sum(r["deleted_at"] is not None for r in store.rows()) == limit
    store.sweep()
    assert all(r["deleted_at"] is not None for r in store.rows())


def test_at_cap_does_no_filesystem_work(store, monkeypatch):
    store.seed(["p/a.jpg"])
    monkeypatch.setattr(store.module, store.cap, 10)

    def forbidden(*args, **kwargs):
        pytest.fail("retention below or at cap touched files")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", forbidden)
        patch.setattr(Path, "rmdir", forbidden)
        store.sweep()
    assert store.rows()[0]["deleted_at"] is None
    assert store.module._stats["evicted_files"] == 0


@pytest.mark.parametrize("main_count", [0, 1])
def test_link_cap_is_independent_when_main_is_within_cap(tmp_path, monkeypatch, main_count):
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(tmp_path / "state.sqlite"))
    import piece_image_store as module

    monkeypatch.setattr(module, "_initialized", False)
    monkeypatch.setattr(module, "_MAX_TOTAL_BYTES", 10)
    monkeypatch.setattr(module, "_MAX_LINK_TOTAL_BYTES", 10)
    with module._connection() as conn:
        if main_count:
            conn.execute("INSERT INTO piece_images (piece_uuid, seq, created_at, bytes, file_path) "
                         "VALUES ('p', 0, 1, 10, 'p/main.jpg')")
        conn.executemany("INSERT INTO piece_link_images (piece_uuid, seq, created_at, bytes, file_path) "
                         "VALUES ('p', ?, ?, 10, ?)", [(0, 1, 'p/a.jpg'), (1, 2, 'p/b.jpg')])
        conn.commit()
    module._retentionSweep()
    with module._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM piece_images WHERE deleted_at IS NOT NULL").fetchone()[0] == 0
        assert [r[0] is not None for r in conn.execute("SELECT deleted_at FROM piece_link_images ORDER BY id")] == [True, False]
    module._initialized = False
