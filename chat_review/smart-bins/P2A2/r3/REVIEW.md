# P2A2 r3 review: imported-event and native-delivery overlap

Implementation remains uncommitted; review pending. This revision corrects [P2A2 r2](../r2/REVIEW.md) (commit `4089e454858a60f312b1826526884bf7c5c65311`). Accepted predecessors remain [P1 r2](../../P1/r2/REVIEW.md) and [P2A1 r2](../../P2A1/r2/REVIEW.md). `P2A2.patch` is the complete P2A2 slice from those predecessors; `P2A2-r2-to-r3.patch` is the two-file correction from the exact submitted r2 source. Both patch chains reconstruct the same current source/test Git tree.

## Correction and evidence

The r2 planner skipped imported events before its direct native-duplicate check, then excluded every anchor-event UUID from residual overlap scanning. An imported recent-piece UUID could therefore coexist with a native delivery and still receive an opening balance.

The planner now exempts only UUIDs for which the direct native-duplicate check actually recorded a conflict. Imported and other skipped own events reach the potential-overlap scan. A same-machine native delivery with a matching event UUID produces an explicit `potential_native_balance_overlap` discrepancy, retaining the raw event references and withholding the affected anchor's contributions and opening balances. The imported UUID is **not** treated as proven physical identity or subtracted from the aggregate. A matching native delivery added after a plan was made changes the plan and prevents both application and exact replay of the old plan.

An imported aggregate without indicated native overlap still has a group opening balance with `historical_unknown` coverage and its existing uncertainty blocker. The prior destination-time and proven-clear separation checks remain. The core schema stays v2, the explicit migration extension stays v2, old extension v1 remains rejected, and initialization, transaction, replay and inactive-runtime boundaries remain unchanged.

## Validation

Python 3.12 frozen environment, with temporary database and configuration paths set before imports; all test SQLite connections and keepers closed. The new targeted regression run against r2 gave **4 failed, 1 passed**: imported overlap, unsafe-plan application/replay, and a conflicting-event skip path failed; the no-overlap control passed. After the correction:

| Command, from `software/sorter/backend` | Result |
| --- | --- |
| `uv run --frozen python -m pytest tests/test_smart_bins_migration.py -q -k 'imported_event_native_overlap or pre_native_import_plan or conflicting_own_event or imported_supplied_uuid_without_overlap'` | **5 passed, 30 deselected** |
| `uv run --frozen python -m pytest tests/test_smart_bins_migration.py -q` | **35 passed** |
| `uv run --frozen python -m pytest tests/test_smart_bins_migration.py tests/test_smart_bins_storage.py tests/test_local_state.py -q` | **70 passed** on final source |

The overlap test applies and reads back the blocker, verifies zero historical/opening credit, verifies unchanged native credit, and compares all relevant legacy source rows. No predecessor, full-backend or frontend tests were rerun.

The ledger remains inactive. Software history is not physical entry proof. An opening balance imported before a later native delivery is not retroactively reconciled by this planning correction; later runtime reconciliation and activation gates remain outside P2A2. No deployment or physical qualification occurred.
