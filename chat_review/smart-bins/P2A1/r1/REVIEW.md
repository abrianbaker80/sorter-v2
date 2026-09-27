# P2A1 review: inactive smart-bin storage foundations

Implementation remains uncommitted; review pending. This is a slice-only patch against accepted [P1 r2](../../P1/r2/REVIEW.md), whose cumulative patch is based on implementation commit `c4e298ebd1a153001355df3d3b0c41cc5446ac8f`. Apply P1 r2, then `P2A1.patch`. A temporary Git index reproduced all seven current source/test blobs in that order. The P1 files retained their accepted hashes.

## Changes

- `software/sorter/backend/smart_bins_storage.py`: explicit version 1 schema initialization in the existing local-state SQLite database. Adds machine/policy/group/slot/assignment/container/cycle, reservation, release-attempt, delivery, opening-balance, audit, discrepancy and external-operation tables. Foreign keys, positive quantity checks, request/route/completion uniqueness, one open cycle per slot/container, and one nonterminal reservation per machine/piece are enforced in SQLite. Intended reservation destination and intended/evidenced delivery destinations cannot be updated. No import or legacy startup path initializes this schema.
- `software/sorter/backend/smart_bins_storage.py`: `initialize_schema()` is rerunnable and rolls back failed DDL; `check_schema_version(conn)` reports an absent schema as version 0 and rejects incomplete/unsupported schema. `critical_transaction(conn=None)` requires version 1, enables foreign keys and `synchronous=FULL` before `BEGIN IMMEDIATE`; the caller must explicitly commit. Uncommitted exits roll back, nested transactions are rejected, and supplied connection settings are restored.
- `software/sorter/backend/piece_records.py`: `initialize_piece_records()` prepares history before a critical transaction. `recordPieceOnConnection(conn, ...)` uses the existing upsert on the caller's connection without opening or committing one. The public `recordPiece(...)` wrapper retains standalone initialization and commit behavior. Correction/feedback fields and the existing COALESCE fields retain their semantics.
- `software/sorter/backend/tests/test_smart_bins_storage.py`: isolated real-SQLite tests cover initialization, rollback, representative legacy row/setting preservation, constraints, transaction configuration/nesting, shared history/ledger atomicity, correction preservation and wrapper behavior.

## Validation

Temporary `LOCAL_STATE_DB_PATH` and `MACHINE_SPECIFIC_PARAMS_PATH` were set before each Python process imported backend modules. Commands ran from `software/sorter/backend` in the frozen Python 3.12 environment.

| Command | Result |
| --- | --- |
| `uv run --frozen python -m pytest tests/test_smart_bins_storage.py -q` | Final run: 5 passed. An intermediate run failed 3 tests because the new version check compared `sqlite3.Row` objects with tuples; fixed by normalizing rows. |
| `uv run --frozen python -m pytest tests/test_local_state.py tests/test_bounded_transfer.py -q` | 75 passed, 1 failed. The existing `test_drain_legacy_metric_snapshot_tables_empties_and_drops` uses `with sqlite3.connect(...)` without closing the connection, then its fixture deletes the database directory; Windows raised `PermissionError` during cleanup. That test code predates this slice and was left unchanged. |

The patch passed `git diff --check`. P1 eligibility tests were not rerun because their source, interfaces and fixtures were unchanged.

## Remaining limits

The ledger is inactive: no backfill, reservation/transition service, runtime cutover, capacity algorithm, exports or physical qualification is included. P2A2 must reconcile legacy lineage and opening balances before any activation. Later slices must enforce transition evidence, state revision/idempotency behavior and atomic delivery accounting. The local-state cleanup failure above remains an existing Windows test issue. No deployed or installed machine state was inspected.
