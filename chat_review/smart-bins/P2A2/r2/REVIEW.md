# P2A2 r2 review: historical attribution and occupancy evidence

Implementation remains uncommitted; review pending. This revision corrects [P2A2 r1](../r1/REVIEW.md) (commit `ee8fa84071fe29829155f96ef27028988c5e4c4d`). Accepted predecessors are [P1 r2](../../P1/r2/REVIEW.md) and [P2A1 r2](../../P2A1/r2/REVIEW.md). `P2A2.patch` is the complete P2A2 slice from those accepted predecessors; `P2A2-r1-to-r2.patch` contains only this correction from the exact submitted r1 source. Both patch chains reconstructed the same current source/test Git tree.

## Corrected evidence rules

- `RunRecorder` writes an independent runtime run ID; `local_state` creates the sorting-session ID. A historical contribution now keeps both IDs and its distributed time. Attribution requires the same piece UUID, machine, destination coordinates, distributed/recorded time, item fields and classification status, a non-dead history record, and an event time within its sorting session. There is no invented run-to-session mapping. Missing or conflicting evidence remains a blocking `unverified_piece_history` discrepancy.
- A zero-count current state with positive aggregates now produces an anchor and a blocking count/group conflict, with no credit. A nonzero aggregate without a state row, or nonempty state/aggregates with a missing, dangling or closed active-session reference, produces a plan-level blocker. Apply refuses plan-level blockers without changing legacy rows. Legitimately empty state and proven clear snapshots remain distinct.
- Before crediting an opening balance, the planner checks native deliveries against unlinked legacy events for the same machine, bin coordinates and epoch. It also treats a native BIN delivery to those coordinates before the anchor as potential overlap unless a recorded intervening clear separates them. Potential overlap creates a blocking discrepancy and withholds the anchor's historical contributions and opening balances; it does not subtract a guessed piece or discard evidence. A later native delivery and a delivery separated by a proven clear leave a nonoverlapping residual usable with its honest `historical_unknown` coverage.

The core smart-bin schema remains version 2. The separate, explicitly installed migration extension is now **version 2**, adding independent run/session/time provenance to historical contributions. Version 1 extensions are rejected during planning, initialization and apply; this revision provides no conversion or backfill of an r1 import. The plan scope is `legacy-bin-backfill-v2`. Existing native/historical duplicate triggers, transaction boundary, stale-plan check and exact replay remain.

## Validation

Python 3.12 frozen environment, with temporary database and machine-configuration paths established before imports; SQLite connections and keepers closed. New regressions first exposed the r1 behavior: **16 failed, 10 passed** after changing the fixture to production-shaped IDs and adding the initial correction cases. After the correction:

| Command, from `software/sorter/backend` | Result |
| --- | --- |
| `uv run --frozen python -m pytest tests/test_smart_bins_migration.py -q` | **30 passed** |
| `uv run --frozen python -m pytest tests/test_smart_bins_migration.py tests/test_smart_bins_storage.py tests/test_local_state.py -q` | **65 passed** on final source |

The tests cover independent IDs, per-piece provenance, machine/destination/time conflicts, hidden aggregates, invalid active-session references, native overlap, nonoverlapping residuals, clear separation, version rejection and unchanged legacy evidence. P1 evidence is reused; no full-backend or frontend run occurred.

The ledger remains inactive. Historical software records do not prove physical entry. Residual opening balances and later native deliveries still need a service-level reconciliation and activation gate in later slices; no runtime cutover, deployment or physical qualification occurred.
