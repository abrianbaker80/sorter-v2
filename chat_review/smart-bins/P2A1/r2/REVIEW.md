# P2A1 r2 review: relational integrity and test cleanup

Implementation remains uncommitted; review pending. This package follows [P2A1 r1](../r1/REVIEW.md) at review commit `0f2d146132b9ca10e086254f8d9655b9718a6b9f`, with accepted [P1 r2](../../P1/r2/REVIEW.md) as the implementation predecessor. `P2A1.patch` is the complete current P2A1 slice against P1 r2; `P2A1-r1-to-r2.patch` contains only this correction against exact P2A1 r1 source. Both patch chains reproduced all seven current P1/P2A1 source and test blobs in temporary Git indexes.

## Corrected behavior

- `smart_bins_storage.py` now creates explicit **schema version 2**. An existing version 1 ledger is rejected with a migration-not-implemented error, without altering it. Initialization remains explicit and the ledger remains inactive. A new unique index on legacy `sorting_sessions(machine_id, id)` supports a composite foreign key so reservations and deliveries cannot name another machine's run; no legacy rows are changed.
- Every new TEXT primary ID is explicitly `NOT NULL`. Delivery INSERT and UPDATE checks compare reservation ID, machine, piece, run, policy, group, quantity and intended destination, including nullable reject fields. The delivery's reservation link cannot be repointed. Once a delivery exists, the referenced reservation facts cannot change. SQLite foreign keys still protect the references from deletion. Evidenced actual destination stays separate from intent; a delivery to a different receiving cycle remains valid after that cycle detaches from its slot.
- `tests/test_smart_bins_storage.py` now closes its SQLite connections on success and failure while preserving commit/rollback behavior. The narrow cleanup change in `tests/test_local_state.py` closes both raw connections in `test_drain_legacy_metric_snapshot_tables_empties_and_drops`; the WAL keeper is still closed by its fixture.
- `critical_transaction(conn=None)`, `initialize_piece_records()`, and `recordPieceOnConnection(conn, ...)` retain their r1 interfaces and semantics. `piece_records.py` is unchanged from r1.

## Validation

Python 3.12 frozen environment; temporary `LOCAL_STATE_DB_PATH` and `MACHINE_SPECIFIC_PARAMS_PATH` were set before backend imports. No operational database was opened.

| Command, from `software/sorter/backend` | Result |
| --- | --- |
| `uv run --frozen python -m pytest tests/test_smart_bins_storage.py -q --tb=line` | Against submitted r1 source with the new tests: 19 failed, 9 passed. The failures reproduced the missing relationship, NULL-ID, cross-machine-run and explicit-version protections. |
| `uv run --frozen python -m pytest tests/test_smart_bins_storage.py -q` | Final: **30 passed**. Intermediate assertion fixes accounted for version 2 and the existing immutable-actual trigger firing first on one update. |
| `uv run --frozen python -m pytest tests/test_local_state.py tests/test_bounded_transfer.py -q` | **76 passed**. The r1 Windows SQLite cleanup failure is resolved. |

The implementation index remains empty and its branch/HEAD remain `sorting-flow-candidate` / `c4e298ebd1a153001355df3d3b0c41cc5446ac8f`. Accepted P1 behavior was left intact; its eligibility suite was not rerun.

## Remaining limits

There is no v1-to-v2 migration, legacy backfill, activation, reservation transition service, delivery accounting service or runtime cutover. P2A2 must reconcile legacy lineage and opening balances before activation. Later services must validate live policy/slot/cycle suitability, actual-delivery evidence, revisions and idempotency, aggregate accounting, and physical custody; these cannot be inferred from the storage constraints alone. No deployed or physical qualification is claimed.
